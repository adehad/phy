# -*- coding: utf-8 -*-

"""Manual clustering GUI component."""


# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------

from functools import partial
import logging

import numpy as np
from six import string_types

from ._history import GlobalHistory
from ._utils import create_cluster_meta
from .clustering import Clustering
from phy.utils import EventEmitter, Bunch, emit
from phy.gui.actions import Actions
from phy.gui.widgets import Table, HTMLWidget, _uniq, Barrier, AsyncTasks

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Utility functions
# -----------------------------------------------------------------------------

def _process_ups(ups):  # pragma: no cover
    """This function processes the UpdateInfo instances of the two
    undo stacks (clustering and cluster metadata) and concatenates them
    into a single UpdateInfo instance."""
    if len(ups) == 0:
        return
    elif len(ups) == 1:
        return ups[0]
    elif len(ups) == 2:
        up = ups[0]
        up.update(ups[1])
        return up
    else:
        raise NotImplementedError()


def _ensure_all_ints(l):
    if (l is None or l == []):
        return
    for i in range(len(l)):
        l[i] = int(l[i])


# -----------------------------------------------------------------------------
# Action flow
# -----------------------------------------------------------------------------

class ActionFlow(EventEmitter):
    """Keep track of all actions and state changes, and defines how selections change
    after an action."""
    def __init__(self):
        super(ActionFlow, self).__init__()
        self._flow = []

    def _make_state(
            self, cluster_ids=None, similar=None, next_cluster=None, next_similar=None,
            state=None):

        # Get or create the Bunch instance.
        state = state or Bunch(type='state')

        if state.get('cluster_ids', None) is None:
            state.cluster_ids = cluster_ids
        if state.get('similar', None) is None:
            state.similar = similar
        if state.get('next_cluster', None) is None:
            state.next_cluster = next_cluster
        if state.get('next_similar', None) is None:
            state.next_similar = next_similar

        _ensure_all_ints(state.cluster_ids)
        _ensure_all_ints(state.similar)

        return state

    def _append(self, state):
        self._flow.append(Bunch(**state))

    def add_state(self, state=None, **kwargs):
        state = state or self._make_state(**kwargs)
        self._append(state)
        return state

    def update_current_state(
            self, cluster_ids=None, similar=None,
            next_cluster=None, next_similar=None):
        state = self.current()
        if not state or state.type != 'state':
            state = self._make_state()
            self.add_state(state)
        self._make_state(cluster_ids=cluster_ids, similar=similar,
                         next_cluster=next_cluster, next_similar=next_similar,
                         state=state)
        self._flow[-1] = state
        assert self.current() == state

    def _add_action(self, name, **kwargs):
        action = Bunch(type='action', name=name, **kwargs)
        self._append(action)
        state = self.state_after(action)
        self.emit('new_state', state)
        self._append(state)
        return state

    def add_merge(self, cluster_ids=None, to=None):
        return self._add_action('merge', cluster_ids=cluster_ids, to=to)

    def add_split(self, old_cluster_ids=None, new_cluster_ids=None):
        return self._add_action(
            'split', old_cluster_ids=old_cluster_ids, new_cluster_ids=new_cluster_ids)

    def add_move(self, cluster_ids=None, group=None):
        return self._add_action(
            'move', cluster_ids=cluster_ids, group=group)

    def add_undo(self, up=None):
        return self._add_action('undo', up=up)

    def add_redo(self, up=None):
        return self._add_action('redo', up=up)

    def current(self):
        if self._flow:
            obj = self._flow[-1]
            logger.log(5, "Current state: %s", obj)
            return obj

    def state_after(self, action):
        return getattr(self, '_state_after_%s' % action.name)(action)

    def _previous_state(self, obj):
        try:
            i = self._index(obj)
        except ValueError:
            return
        if i == 0:
            return
        for k in range(1, 10):
            previous = self._flow[i - k]
            if previous.type == 'state':
                return previous

    def _last_undo(self):
        for obj in self._flow[::-1]:
            if obj.type == 'action' and obj.name == 'undo':
                return obj

    def _state_after_merge(self, action):
        previous_state = self._previous_state(action)
        similar = previous_state.next_similar
        return self._make_state(
            cluster_ids=[action.to],
            similar=[similar] if similar is not None else None,
        )

    def _state_after_split(self, action):
        return self._make_state(cluster_ids=action.new_cluster_ids)

    def _state_after_move(self, action):
        state = self._previous_state(action)
        next_state = self._make_state()
        moved_clusters = set(action.cluster_ids)
        if moved_clusters <= set(state.cluster_ids):
            if state.next_cluster is not None:
                next_state.cluster_ids = [state.next_cluster]
        elif moved_clusters <= set(state.similar or []):
            next_state.cluster_ids = state.cluster_ids
            if state.next_similar is not None:
                next_state.similar = [state.next_similar]
        else:
            # next_best, and then select next similar.
            next_state.wizard = 'next_best'
            next_state.similar = [state.next_similar]
        return self._make_state(state=next_state)

    def _state_after_undo(self, action):
        return self._previous_state(self._previous_state(action))

    def _state_after_redo(self, action):
        undo = self._last_undo()
        if undo:
            return self._previous_state(undo)

    def to_json(self):
        # TODO
        return {}

    def _index(self, item):
        return len(self._flow) - 1 - self._flow[::-1].index(item)

    def _show_state(self, state):
        s = ('#{i:03d}   {cluster_ids: <8}  ({next_cluster: <3})   '
             '{similar: <8}  ({next_similar: <3})')
        s = s.format(
            i=self._index(state),
            cluster_ids=str(state.cluster_ids),
            next_cluster=str(state.next_cluster),
            similar=str(state.similar),
            next_similar=str(state.next_similar),
        )
        logger.debug(s)

    def _show_action(self, action):
        s = '  '.join('%s:%s' % (key, val) for key, val in action.items()
                      if key not in ('name', 'type'))
        s = '#{i:03d}   {name} {s}'.format(i=self._index(action), name=action.name, s=s)
        logger.debug(s)

    def show_last(self, n=5):
        length = len(self._flow)
        logger.debug("Last %d/%d" % (min(n, length), length))
        for item in self._flow[-n:]:
            if item.type == 'state':
                self._show_state(item)
            elif item.type == 'action':
                self._show_action(item)


# -----------------------------------------------------------------------------
# Cluster view and similarity view
# -----------------------------------------------------------------------------

class ClusterView(Table):
    def __init__(self, *args, data=None):
        HTMLWidget.__init__(self, *args, title='ClusterView')
        self._set_styles()
        emit('cluster_view_init', self)

        extra_columns = emit('request_cluster_metrics', self, single=True)
        columns = ['id', 'n_spikes', 'quality'] + extra_columns

        assert columns[0] == 'id'

        # Allow to have <tr data_group="good"> etc. which allows for CSS styling.
        value_names = columns + [{'data': ['group']}]
        self._init_table(columns=columns, value_names=value_names, data=data)

        @self.connect_
        def on_ready():
            self.sort_by('quality', 'desc')

    def _set_styles(self):
        self.builder.add_style('''
            table tr[data-group='good'] {
                color: #86D16D;
            }

            table tr[data-group='mua'], table tr[data-group='noise'] {
                color: #888;
            }
            ''')

    def get_state(self, callback=None):
        self.get_current_sort(lambda sort: callback({'current_sort': tuple(sort)}))

    def set_state(self, state):
        sort_by, sort_dir = state.get('current_sort', (None, None))
        if sort_by:
            self.sort_by(sort_by, sort_dir)


class SimilarityView(ClusterView):
    """Must connect request_similar_clusters."""
    def __init__(self, *args, data=None):
        HTMLWidget.__init__(self, *args, title='SimilarityView')
        self._set_styles()
        emit('similarity_view_init', self)
        extra_columns = emit('request_cluster_metrics', self, single=True)
        columns = ['id', 'n_spikes', 'similarity'] + extra_columns
        value_names = columns + [{'data': ['group']}]
        self._init_table(columns=columns, value_names=value_names, data=data)

        @self.connect_
        def on_ready():
            self.sort_by('similarity', 'desc')

    def reset(self, cluster_ids):
        if not len(cluster_ids):
            return
        similar = self.emit('request_similar_clusters', cluster_ids[-1])
        # Clear the table.
        if similar:
            self.remove_all_and_add(
                [cl for cl in similar[0] if cl['id'] not in cluster_ids])
        else:
            self.remove_all()
        return similar


# -----------------------------------------------------------------------------
# ActionCreator
# -----------------------------------------------------------------------------

class ActionCreator(EventEmitter):
    default_shortcuts = {
        # Clustering.
        'merge': 'g',
        'split': 'k',

        'label': 'l',

        # Move.
        'move_best_to_noise': 'alt+n',
        'move_best_to_mua': 'alt+m',
        'move_best_to_good': 'alt+g',
        'move_best_to_unsorted': 'alt+u',

        'move_similar_to_noise': 'ctrl+n',
        'move_similar_to_mua': 'ctrl+m',
        'move_similar_to_good': 'ctrl+g',
        'move_similar_to_unsorted': 'ctrl+u',

        'move_all_to_noise': 'ctrl+alt+n',
        'move_all_to_mua': 'ctrl+alt+m',
        'move_all_to_good': 'ctrl+alt+g',
        'move_all_to_unsorted': 'ctrl+alt+u',

        # Wizard.
        'reset': 'ctrl+alt+space',
        'next': 'space',
        'previous': 'shift+space',
        'next_best': 'down',
        'previous_best': 'up',

        # Misc.
        'save': 'Save',
        'show_shortcuts': 'Save',
        'undo': 'Undo',
        'redo': ('ctrl+shift+z', 'ctrl+y'),
    }

    def add(self, name, **kwargs):
        # This special keyword argument lets us use a different name for the
        # action and the event name/method (used for different move flavors).
        method_name = kwargs.pop('method_name', name)
        method_args = kwargs.pop('method_args', ())
        emit_fun = partial(self.emit, 'action', method_name, *method_args)
        self.actions.add(emit_fun, name=name, **kwargs)

    def separator(self, **kwargs):
        self.actions.separator(**kwargs)

    def attach(self, gui):
        self.actions = Actions(gui,
                               name='Clustering',
                               menu='&Clustering',
                               default_shortcuts=self.default_shortcuts)

        # Selection.
        self.add('select', alias='c', docstring='Select some clusters.')
        self.separator()

        self.add('undo', docstring='Undo the last action.')
        self.add('redo', docstring='Redo the last undone action.')
        self.separator()

        # Clustering.
        self.add('merge', alias='g', docstring='Merge the selected clusters.')
        self.add('split', alias='k', docstring='Create a new cluster out of the selected spikes')
        self.separator()

        # Move.
        self.add('move', docstring='Move some clusters to a group.')
        self.separator()

        for which in ('best', 'similar', 'all'):
            for group in ('noise', 'mua', 'good', 'unsorted'):
                self.add('move_%s_to_%s' % (which, group),
                         method_name='move',
                         method_args=(group, which),
                         docstring='Move %s to %s.' % (which, group))
            self.separator()

        # Label.
        self.add('label', alias='l', docstring='Label the selected clusters.')

        # Others.
        self.add('save', menu='&File', docstring='Save all pending changes.')

        # Wizard.
        self.add('reset', menu='&Wizard', docstring='Reset the wizard.')
        self.separator(menu='&Wizard')
        self.add('next', menu='&Wizard', docstring='Select the next similar cluster.')
        self.add('previous', menu='&Wizard', docstring='Select the previous similar cluster.')
        self.separator(menu='&Wizard')
        self.add('next_best', menu='&Wizard', docstring='Select the next best cluster.')
        self.add('previous_best', menu='&Wizard', docstring='Select the previous best cluster.')
        self.separator(menu='&Wizard')


# -----------------------------------------------------------------------------
# Clustering GUI component
# -----------------------------------------------------------------------------

def _is_group_masked(group):
    return group in ('noise', 'mua')


class Supervisor(EventEmitter):
    """Component that brings manual clustering facilities to a GUI:

    * Clustering instance: merge, split, undo, redo
    * ClusterMeta instance: change cluster metadata (e.g. group)
    * Selection
    * Many manual clustering-related actions, snippets, shortcuts, etc.

    Parameters
    ----------

    spike_clusters : ndarray
    cluster_groups : dictionary
    quality: func
    similarity: func
    new_cluster_id: func
    context: Context instance

    GUI events
    ----------

    When this component is attached to a GUI, the GUI emits the following
    events:

    select(cluster_ids)
        when clusters are selected
    cluster(up)
        when a merge or split happens
    request_save(spike_clusters, cluster_groups)
        when a save is requested by the user

    """

    def __init__(self,
                 spike_clusters=None,
                 cluster_groups=None,
                 quality=None,
                 similarity=None,
                 new_cluster_id=None,
                 context=None,
                 ):
        super(Supervisor, self).__init__()
        self._pause_action_flow = None
        self.context = context
        self.quality = quality or self.n_spikes  # function cluster => quality
        self.similarity = similarity  # function cluster => [(cl, sim), ...]

        # Create Clustering and ClusterMeta.
        # Load the cached spikes_per_cluster array.
        spc = context.load('spikes_per_cluster') if context else None
        self.clustering = Clustering(spike_clusters,
                                     spikes_per_cluster=spc,
                                     new_cluster_id=new_cluster_id)
        # Cache the spikes_per_cluster array.
        self._save_spikes_per_cluster()

        # Create the ClusterMeta instance.
        self.cluster_meta = create_cluster_meta(cluster_groups or {})

        # Create the GlobalHistory instance.
        self._global_history = GlobalHistory(process_ups=_process_ups)

        # Create the Action Flow instance.
        self.action_flow = ActionFlow()
        # Call _select_after_action when ActionFlow requests a new state
        # after an action.
        self.action_flow.connect(self._select_after_action, event='new_state')

        # Create The Action Creator instance.
        self.action_creator = ActionCreator()
        self.action_creator.connect(self._on_action, event='action')

        # Log the actions.
        self.clustering.connect(self._log_action, event='cluster')
        self.cluster_meta.connect(self._log_action_meta, event='cluster')

        # Raise the global cluster event.
        self.clustering.connect(partial(self.emit, 'cluster'), event='cluster')
        self.cluster_meta.connect(partial(self.emit, 'cluster'), event='cluster')

        # Create the cluster view and similarity view.
        self._create_views()

    # Internal methods
    # -------------------------------------------------------------------------

    def _save_spikes_per_cluster(self):
        if not self.context:
            return
        self.context.save('spikes_per_cluster',
                          self.clustering.spikes_per_cluster,
                          kind='pickle',
                          )

    def _log_action(self, up):
        if up.history:
            logger.info(up.history.title() + " cluster assign.")
        elif up.description == 'merge':
            logger.info("Merge clusters %s to %s.",
                        ', '.join(map(str, up.deleted)),
                        up.added[0])
        else:
            logger.info("Assigned %s spikes.", len(up.spike_ids))
        #self.emit('cluster', up)

    def _log_action_meta(self, up):
        if up.history:
            logger.info(up.history.title() + " move.")
        else:
            logger.info("Change %s for clusters %s to %s.",
                        up.description,
                        ', '.join(map(str, up.metadata_changed)),
                        up.metadata_value)

        # Skip cluster metadata other than groups.
        if up.description != 'metadata_group':
            return

    def _save_new_cluster_id(self, up):
        # Save the new cluster id on disk.
        new_cluster_id = self.clustering.new_cluster_id()
        if self.context:
            logger.debug("Save the new cluster id: %d.", new_cluster_id)
            self.context.save('new_cluster_id',
                              dict(new_cluster_id=new_cluster_id))

    def _save_gui_state(self, gui):
        gui.state.update_view_state(self.cluster_view, self.cluster_view.state)
        # NOTE: create_gui() already saves the state, but the event
        # is registered *before* we add all views.
        gui.state.save()

    def n_spikes(self, cluster_id):
        return len(self.clustering.spikes_per_cluster.get(cluster_id, []))

    def _get_similar_clusters(self, cluster_id):
        sim = self.similarity(cluster_id)
        # Only keep existing clusters.
        clusters_set = set(self.clustering.cluster_ids)
        data = [dict(similarity='%.3f' % s,
                     **self._get_cluster_info(c, exclude=('quality',)))
                for c, s in sim
                if c in clusters_set]
        return data

    def _get_cluster_info(self, cluster_id, exclude=()):
        group = self.cluster_meta.get('group', cluster_id)
        out = {'id': cluster_id,
               'n_spikes': self.n_spikes(cluster_id),
               'quality': '%.3f' % self.quality(cluster_id),
               'group': group,
               'is_masked': _is_group_masked(group),
               }
        return {k: v for k, v in out.items() if k not in exclude}

    def _create_views(self, gui=None):
        data = [self._get_cluster_info(cluster_id) for cluster_id in self.clustering.cluster_ids]
        self.cluster_view = ClusterView(gui, data=data)
        # Update the action flow and similarity view when selection changes.
        self.cluster_view.connect_(self._clusters_selected, event='select')

        self.similarity_view = SimilarityView(gui)
        self.similarity_view.connect_(self._get_similar_clusters, event='request_similar_clusters')
        self.similarity_view.connect_(self._similar_selected, event='select')

        # Change the state after every clustering action, according to the action flow.
        self.connect(self._after_action, event='cluster')

    def _clusters_added(self, cluster_ids):
        logger.log(5, "Clusters added: %s", cluster_ids)
        data = [self._get_cluster_info(cluster_id) for cluster_id in cluster_ids]
        self.cluster_view.add(data)
        self.similarity_view.add(data)

    def _clusters_removed(self, cluster_ids):
        logger.log(5, "Clusters removed: %s", cluster_ids)
        self.cluster_view.remove(cluster_ids)
        self.similarity_view.remove(cluster_ids)

    def _cluster_groups_changed(self, cluster_ids):
        logger.log(5, "Cluster groups changed: %s", cluster_ids)
        data = [{'id': cluster_id,
                 'group': self.cluster_meta.get('group', cluster_id),
                }
                for cluster_id in cluster_ids]
        for _ in data:
            _['is_masked'] = _is_group_masked(_['group'])
        self.cluster_view.change(data)
        self.similarity_view.change(data)

    def _update_next_cluster(self, view):
        current = self.action_flow.current()
        if current.type != 'state':
            return
        if not current.get('next_cluster', None):
            view.get_next_id(
                lambda next_cluster: current.update(next_cluster=next_cluster))

    def _clusters_selected(self, cluster_ids_and_next):
        cluster_ids, next_cluster = cluster_ids_and_next
        logger.debug("Clusters selected: %s (%s)", cluster_ids, next_cluster)
        if not self._pause_action_flow:
            self.action_flow.add_state(cluster_ids=cluster_ids, next_cluster=next_cluster)
        self.similarity_view.reset(cluster_ids)
        self.emit('select_clusters_done')
        self.action_flow.show_last()

    def _similar_selected(self, similar_and_next):
        similar, next_similar = similar_and_next
        logger.debug("Similar clusters selected: %s (%s)", similar, next_similar)
        current = self.action_flow.current()
        if not self._pause_action_flow:
            self.action_flow.add_state(
                cluster_ids=current.cluster_ids, next_cluster=current.next_cluster,
                similar=similar, next_similar=next_similar)
        self.emit('select_similar_done')
        self.action_flow.show_last()

    def _on_action(self, name, *args):
        """Bind the 'action' event raised by ActionCreator to methods of this class."""
        return getattr(self, name)(*args)

    def _select_after_action(self, state):
        if state.type != 'state':
            return
        self._pause_action_flow = True

        def _select_done(similar_and_next=(None, None)):
            similar, next_similar = similar_and_next
            if not state.next_similar:
                self.action_flow.update_current_state(next_similar=next_similar)
            self._pause_action_flow = False
            self.emit('select_after_action_done')
            self.action_flow.show_last()

        def _select_similar(cluster_ids_and_next=(None, None)):
            cluster_ids, next_cluster = cluster_ids_and_next
            if not state.next_cluster:
                self.action_flow.update_current_state(
                    cluster_ids=state.cluster_ids or cluster_ids,
                    next_cluster=next_cluster)
            if state.similar:
                self.similarity_view.select(state.similar, callback=_select_done)
            else:
                _select_done()

        if state.cluster_ids:
            self.cluster_view.select(state.cluster_ids, callback=_select_similar)

        # wizard field is a method name to call after the action.
        wizard = state.get('wizard', None)
        if not wizard:
            return
        getattr(self, wizard)(callback=_select_similar)

    def _after_action(self, up):
        # Update the views with the old and new clusters.
        self._clusters_added(up.added)
        self._clusters_removed(up.deleted)

        # Prepare the next selection after the action.
        if up.history == 'undo':
            self.action_flow.add_undo(up)
        elif up.history == 'redo':
            self.action_flow.add_redo(up)
        elif up.description == 'merge':
            self.action_flow.add_merge(up.deleted, up.added[0])
        elif up.description == 'assign':
            self.action_flow.add_split(old_cluster_ids=up.deleted,
                                       new_cluster_ids=up.added)
        elif up.description == 'metadata_group':
            self._cluster_groups_changed(up.metadata_changed)
            self.action_flow.add_move(up.metadata_changed, up.metadata_value)

        #self.emit('cluster', up)
        # New selection done by ActionFlow which emits "new_state", connected
        # to _select_after_action.

    @property
    def state(self):
        b = Barrier()
        self.cluster_view.get_state(b(1))
        self.similarity_view.get_state(b(2))
        b.wait()
        sc = b.result(1)[0][0]
        ss = b.result(2)[0][0]
        return Bunch({'cluster_view': Bunch(sc), 'similarity_view': Bunch(ss)})

    def attach(self, gui):

        self.cluster_view.set_state(gui.state.get_view_state(self.cluster_view))
        gui.add_view(self.cluster_view)
        gui.add_view(self.similarity_view)

        self.action_creator.attach(gui)

    @property
    def actions(self):
        return self.action_creator.actions

    # Selection actions
    # -------------------------------------------------------------------------

    def select(self, *cluster_ids):
        """Select a list of clusters."""
        # HACK: allow for `select(1, 2, 3)` in addition to `select([1, 2, 3])`
        # This makes it more convenient to select multiple clusters with
        # the snippet: `:c 1 2 3` instead of `:c 1,2,3`.
        if cluster_ids and isinstance(cluster_ids[0], (tuple, list)):
            cluster_ids = list(cluster_ids[0]) + list(cluster_ids[1:])
        # Remove non-existing clusters from the selection.
        #cluster_ids = self._keep_existing_clusters(cluster_ids)
        # Update the cluster view selection.
        self.cluster_view.select(cluster_ids)

    '''
    def _wait_selected(self, view):
        b = Barrier()
        view.get_selected(b(1))
        b.wait()
        return b.result(1)[0][0]

    def get_selected(self, callback=None):
        """Get the selected clusters in the cluster and similarity views.
        Asynchronous operation if no callback is passed, otherwise synchronous."""
        if callback is None:
            cluster_ids = self._wait_selected(self.cluster_view)
            similar = self._wait_selected(self.similarity_view)
            return _uniq(cluster_ids + similar)

        b = Barrier()
        self.cluster_view.get_selected(b('cluster_view'))
        self.similarity_view.get_selected(b('similarity_view'))

        @b.after_all_finished
        def _callback_after_both():
            cluster_ids = b.result('cluster_view')[0][0]
            similar = b.result('similarity_view')[0][0]
            selected = _uniq(cluster_ids + similar)
            callback(selected)
    '''

    # Clustering actions
    # -------------------------------------------------------------------------

    def selected_clusters(self):
        state = self.action_flow.current()
        return state.cluster_ids or [] if state else []

    def selected_similar(self):
        state = self.action_flow.current()
        return state.similar or [] if state else []

    def selected(self):
        return _uniq(self.selected_clusters() + self.selected_similar())

    def merge(self, cluster_ids=None, to=None):
        """Merge the selected clusters."""
        if cluster_ids is None:
            cluster_ids = self.selected()
        if len(cluster_ids or []) <= 1:
            return
        self.clustering.merge(cluster_ids, to=to)
        self._global_history.action(self.clustering)

    def split(self, spike_ids=None, spike_clusters_rel=0):
        """Split the selected spikes."""
        if spike_ids is None:
            spike_ids = self.emit('request_split', single=True)
            spike_ids = np.asarray(spike_ids, dtype=np.int64)
            assert spike_ids.dtype == np.int64
            assert spike_ids.ndim == 1
        if len(spike_ids) == 0:
            msg = ("You first need to select spikes in the feature "
                   "view with a few Ctrl+Click around the spikes "
                   "that you want to split.")
            self.emit('error', msg)
            return
        self.clustering.split(spike_ids,
                              spike_clusters_rel=spike_clusters_rel)
        self._global_history.action(self.clustering)

    # Move actions
    # -------------------------------------------------------------------------

    @property
    def fields(self):
        """Tuple of label fields."""
        return tuple(f for f in self.cluster_meta.fields
                     if f not in ('group',))

    def get_labels(self, field):
        """Return the labels of all clusters, for a given field."""
        return {c: self.cluster_meta.get(field, c)
                for c in self.clustering.cluster_ids}

    def label(self, name, value, cluster_ids=None):
        """Assign a label to clusters.

        Example: `quality 3`

        """
        if cluster_ids is None:
            cluster_ids = self.selected()
        if not hasattr(cluster_ids, '__len__'):
            cluster_ids = [cluster_ids]
        if len(cluster_ids) == 0:
            return
        self.cluster_meta.set(name, cluster_ids, value)
        self._global_history.action(self.cluster_meta)

    def move(self, group, which):
        """Assign a group to some clusters."""
        if which == 'all':
            which = self.selected()
        elif which == 'best':
            which = self.selected_clusters()
        elif which == 'similar':
            which = self.selected_similar()
        if isinstance(which, string_types):
            logger.warn("The list of clusters should be a list of integers, "
                        "not a string.")
            return
        if not which:
            return
        assert which
        logger.debug("Move %s to %s.", which, group)
        if group == 'unsorted':
            group = None
        self.label('group', group, cluster_ids=which)

    # Wizard actions
    # -------------------------------------------------------------------------

    def reset(self, callback=None):
        """Reset the wizard."""
        self.cluster_view.first(callback=callback or partial(self.emit, 'wizard_done'))

    def next_best(self, callback=None):
        """Select the next best cluster."""
        self.cluster_view.next(callback=callback or partial(self.emit, 'wizard_done'))

    def previous_best(self, callback=None):
        """Select the previous best cluster."""
        self.cluster_view.previous(callback=callback or partial(self.emit, 'wizard_done'))

    def next(self, callback=None):
        """Select the next cluster."""
        state = self.action_flow.current()
        if not state or not state.cluster_ids:
            self.cluster_view.first(callback=callback or partial(self.emit, 'wizard_done'))
        else:
            self.similarity_view.next(callback=callback or partial(self.emit, 'wizard_done'))

    def previous(self, callback=None):
        """Select the previous cluster."""
        self.similarity_view.previous(callback=callback or partial(self.emit, 'wizard_done'))

    # Other actions
    # -------------------------------------------------------------------------

    def undo(self):
        """Undo the last action."""
        self._global_history.undo()

    def redo(self):
        """Undo the last undone action."""
        self._global_history.redo()

    def save(self):
        """Save the manual clustering back to disk."""
        spike_clusters = self.clustering.spike_clusters
        groups = {c: self.cluster_meta.get('group', c) or 'unsorted'
                  for c in self.clustering.cluster_ids}
        # List of tuples (field_name, dictionary).
        labels = [(field, self.get_labels(field))
                  for field in self.cluster_meta.fields
                  if field not in ('next_cluster')]
        # TODO: add option in add_field to declare a field unsavable.
        self.emit('request_save', spike_clusters, groups, *labels)
        # Cache the spikes_per_cluster array.
        self._save_spikes_per_cluster()
