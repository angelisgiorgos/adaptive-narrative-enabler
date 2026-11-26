"""The engine context: the frozen configuration one game, training run or evaluation runs with.

An ``EngineContext`` is taken when something starts (a game, a training run, an evaluation) and
never changes afterwards: its world and tuning (a private deep copy of the configuration), the
storyteller (genome traits), the training hyperparameters, the tag vectors and their caches (a
``TagSpace``), a world version (a digest of the configuration) and the seed.

    context = EngineContext.capture(genome, seed=3)      # the process-wide configuration, now
    context = model.context(seed=3)                      # a saved engine's own (EngineModel)
    with context.bind():                                 # config.get / tag similarity read it
        world = build_world(genome)
        state = GameState(world, genome, rng=random.Random(3))   # keeps the bound context

A GameState keeps the context it was created under (or one captured then) and binds it on every
call, so the rules it runs read that context whatever else happens in the process: another
engine activated for another session, an editor saving the files, an upload. ``config.get``
(utils/config_loader.py) reads the bound context, so the engine's modules read it without change.

Binding is per thread (a ``contextvars.ContextVar``): a thread pool started inside a bound block
does not inherit it, so work handed to one goes through ``propagate`` (or binds the context
itself).
"""
from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import json
from types import MappingProxyType

from utils.config_loader import ACTIVE_CONTEXT, config, lookup


def _digest(data):
    payload = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _frozen(value):
    """A read-only view of a configuration value (mappings and lists, recursively)."""
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


class EngineContext:
    """The frozen runtime configuration of one game, training run or evaluation.

    ``config_data``: the complete configuration (world and tuning; copied, never shared);
    ``genome``: the storyteller (a Genome or its traits; None for a context that is not one
    storyteller's, e.g. a training run's); ``tag_vectors``: pre-computed tag vectors (a trained
    engine's), else every tag is encoded by the embedding model; ``tag_space``: a TagSpace to use
    instead (its vectors); ``seed``: the seed of what runs with it (provenance; the dice are the
    caller's); ``source``: what it was taken from (for messages)."""

    __slots__ = ("_data", "_genome", "_tag_space", "_tag_vectors_key", "_world_version", "_seed", "_source",
                 "_frozen_views", "__weakref__")

    def __init__(self, config_data, genome=None, tag_vectors=None, tag_space=None, seed=None, source=""):
        from utils.tag_similarity import DEFAULT_MODEL_NAME, shared_space

        data = copy.deepcopy(dict(config_data or {}))  # exactly as given: the same digest as the source
        object.__setattr__(self, "_data", data)
        traits = genome.traits() if hasattr(genome, "traits") else (dict(genome) if genome else None)
        object.__setattr__(self, "_genome", MappingProxyType({k: float(v) for k, v in traits.items()})
                           if traits is not None else None)
        if tag_space is None:
            model_name = lookup(data, "semantic_similarity.model_name", DEFAULT_MODEL_NAME)
            tag_space = shared_space(tag_vectors or None, model_name=model_name)
        object.__setattr__(self, "_tag_space", tag_space)
        object.__setattr__(self, "_world_version", _digest(data))
        object.__setattr__(self, "_seed", seed)
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_frozen_views", {})

    def __setattr__(self, name, value):
        raise AttributeError("An EngineContext is immutable: take a new one (with_genome, with_seed, capture).")

    def __delattr__(self, name):
        raise AttributeError("An EngineContext is immutable.")

    # ---- taking one -----------------------------------------------------------------------------

    @classmethod
    def capture(cls, genome=None, seed=None, source="process configuration"):
        """The process-wide configuration as it is now (the files, or the override in use) and the
        process-wide tag vectors, frozen."""
        from utils.tag_similarity import default_space, shared_space

        data = config.process_dict()
        space = default_space()
        vectors = space.preloaded
        model_name = lookup(data, "semantic_similarity.model_name", None)
        return cls(data, genome=genome, tag_space=shared_space(vectors or None, model_name=model_name),
                   seed=seed, source=source)

    @classmethod
    def current(cls, genome=None, seed=None):
        """The bound context (with ``genome`` / ``seed`` if given and different), else a capture."""
        bound = ACTIVE_CONTEXT.get()
        if bound is None:
            return cls.capture(genome, seed=seed)
        if genome is not None:
            bound = bound.with_genome(genome)
        if seed is not None and seed != bound.seed:
            bound = bound.with_seed(seed)
        return bound

    def _derived(self, genome, seed):
        twin = EngineContext.__new__(EngineContext)
        for name in ("_data", "_tag_space", "_world_version", "_source"):
            object.__setattr__(twin, name, getattr(self, name))  # shared: never changed
        traits = genome.traits() if hasattr(genome, "traits") else (dict(genome) if genome else None)
        object.__setattr__(twin, "_genome", MappingProxyType({k: float(v) for k, v in traits.items()})
                           if traits is not None else None)
        object.__setattr__(twin, "_seed", seed)
        object.__setattr__(twin, "_frozen_views", self._frozen_views)
        return twin

    def with_genome(self, genome):
        """This context told by another storyteller (the same configuration and tag vectors)."""
        traits = genome.traits() if hasattr(genome, "traits") else dict(genome or {})
        if self._genome is not None and dict(self._genome) == {k: float(v) for k, v in traits.items()}:
            return self
        return self._derived(genome, self._seed)

    def with_seed(self, seed):
        return self if seed == self._seed else self._derived(self._genome, seed)

    # ---- reading it -----------------------------------------------------------------------------

    def get(self, key, default=None):
        """The value at the dotted ``key`` (as ``config.get``). Treat it as read-only."""
        return lookup(self._data, key, default)

    def as_dict(self):
        """A copy of the configuration (yours to change)."""
        return copy.deepcopy(self._data)

    def _view(self, name, value):
        views = self._frozen_views
        if name not in views:
            views[name] = _frozen(copy.deepcopy(value))
        return views[name]

    @property
    def world(self):
        """The world definition (read-only)."""
        return self._view("world", self._data.get("world_definition") or {})

    @property
    def hyperparameters(self):
        """The training hyperparameters (the ``evolution`` section; read-only)."""
        return self._view("evolution", self._data.get("evolution") or {})

    @property
    def settings(self):
        """The whole configuration (read-only)."""
        return self._view("all", self._data)

    @property
    def genome_traits(self):
        """The storyteller's traits (read-only), or None."""
        return self._genome

    @property
    def genome(self):
        from .genome import Genome

        return None if self._genome is None else Genome.from_traits(dict(self._genome))

    @property
    def tag_space(self):
        return self._tag_space

    @property
    def tag_vectors(self):
        """The pre-computed tag vectors this context reads ({} when tags are encoded on demand)."""
        return self._tag_space.preloaded

    @property
    def world_version(self):
        """A digest of the configuration (world and tuning) this context was taken with."""
        return self._world_version

    @property
    def seed(self):
        return self._seed

    @property
    def source(self):
        return self._source

    def unchanged(self):
        """Whether the configuration still has the content it was taken with (nothing wrote into it)."""
        return _digest(self._data) == self._world_version

    def describe(self):
        return {"world_version": self._world_version, "seed": self._seed, "source": self._source,
                "storyteller": dict(self._genome) if self._genome is not None else None,
                "tag_space": list(self._tag_space.key)}

    def __repr__(self):
        return f"EngineContext(world_version={self._world_version!r}, seed={self._seed!r}, source={self._source!r})"

    # ---- binding --------------------------------------------------------------------------------

    @contextlib.contextmanager
    def bind(self):
        """Within the block, in this thread, ``config.get`` / ``config.as_dict`` and tag similarity
        read this context."""
        token = ACTIVE_CONTEXT.set(self)
        try:
            yield self
        finally:
            ACTIVE_CONTEXT.reset(token)

    # ---- copying and pickling: immutable, so a copy is itself; a pickle carries the content ------

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (_restore, (self._data, dict(self._genome) if self._genome is not None else None,
                           self._tag_space.preloaded, self._seed, self._source))


def _restore(config_data, genome, tag_vectors, seed, source):
    return EngineContext(config_data, genome=genome, tag_vectors=tag_vectors, seed=seed, source=source)


class _ProcessConfiguration:
    """An engine context that reads the process-wide configuration live (not a frozen copy): what
    every game read before engine contexts existed. Only follow_process_config uses it."""

    source = "process configuration (live)"
    seed = None
    genome_traits = None

    @property
    def _data(self):
        return config._config

    @property
    def tag_space(self):
        from utils.tag_similarity import default_space

        return default_space()

    def get(self, key, default=None):
        return lookup(config._config, key, default)

    def as_dict(self):
        return config.process_dict()

    @property
    def world_version(self):
        return _digest(config._config)

    def with_genome(self, genome):
        return self

    def with_seed(self, seed):
        return self

    def bind(self):
        return EngineContext.bind(self)

    def __deepcopy__(self, memo):
        return self


@contextlib.contextmanager
def follow_process_config(*states):
    """Tests and debugging tools only: while the block runs, ``states`` (GameStates) read the
    process-wide configuration live, as it is at each call, instead of their frozen context; e.g. to
    probe a story already set up under a changed setting::

        with patch.dict(config._config["director"], {"enabled": False}), follow_process_config(state):
            names = [a.name for a in state.player_options()]

    Their own context is back afterwards. Never use it for real play: a game must keep the
    configuration it started with."""
    live = _ProcessConfiguration()
    kept = [state.__dict__.get("_context") for state in states]
    for state in states:
        state.__dict__["_context"] = live
    try:
        with EngineContext.bind(live):
            yield
    finally:
        for state, context in zip(states, kept):
            if context is None:
                state.__dict__.pop("_context", None)
            else:
                state.__dict__["_context"] = context


def current_context():
    """The bound EngineContext, or None (the process-wide configuration is read)."""
    return ACTIVE_CONTEXT.get()


def bound(context):
    """``context.bind()``, or nothing to do for None."""
    return context.bind() if context is not None else contextlib.nullcontext()


def propagate(function):
    """``function`` running in the engine context bound now, in whichever thread calls it (thread
    pools do not inherit it). Picklable when ``function`` is: a process pool receives the bare
    function, and its processes read the configuration they were started with (main.story_pool)."""
    context = ACTIVE_CONTEXT.get()
    return function if context is None else _InContext(function, context)


class _InContext:
    __slots__ = ("function", "context", "__weakref__")

    def __init__(self, function, context):
        self.function = function
        self.context = context

    def __call__(self, *args, **kwargs):
        token = ACTIVE_CONTEXT.set(self.context)
        try:
            return self.function(*args, **kwargs)
        finally:
            ACTIVE_CONTEXT.reset(token)

    def __reduce__(self):
        return (_bare, (self.function,))


def _bare(function):
    return function


def state_context(state):
    """The engine context of a GameState (captured now for one made without)."""
    return getattr(state, "context", None)


def with_state_context(function):
    """Decorate a module function whose first argument is a GameState: it runs in that state's
    engine context (e.g. resume.parameters, snapshot.dump, fitness)."""

    @functools.wraps(function)
    def run(state, *args, **kwargs):
        context = state_context(state)
        if context is None or ACTIVE_CONTEXT.get() is context:
            return function(state, *args, **kwargs)
        token = ACTIVE_CONTEXT.set(context)
        try:
            return function(state, *args, **kwargs)
        finally:
            ACTIVE_CONTEXT.reset(token)

    return run


def binds_own_context(context_of, private=(), slot=None):
    """Class decorator: the class's public methods (its own and inherited; not dunders, static or
    class methods, or properties) and the ``private`` ones named (those other modules call) run in
    the engine context ``context_of(self)`` (None: as called). Private methods called only from
    the class's own methods run in the context those bound already (no cost per call).
    ``slot``: the instance attribute that holds the context, read first (the fast path)."""
    def decorate(cls):
        seen = set()
        for klass in cls.__mro__:
            if klass is object:
                continue
            for name, value in vars(klass).items():
                if name in seen or name.startswith("__") or (name.startswith("_") and name not in private):
                    continue
                seen.add(name)
                if not callable(value) or isinstance(value, (staticmethod, classmethod, property, type)):
                    continue
                setattr(cls, name, _binding_method(value, context_of, slot))
        return cls

    return decorate


def _binding_method(method, context_of, slot):
    active = ACTIVE_CONTEXT.get

    @functools.wraps(method)
    def run(self, *args, **kwargs):
        context = self.__dict__.get(slot) if slot is not None else None
        if context is None:
            context = context_of(self)
        if context is None or active() is context:
            return method(self, *args, **kwargs)
        token = ACTIVE_CONTEXT.set(context)
        try:
            return method(self, *args, **kwargs)
        finally:
            ACTIVE_CONTEXT.reset(token)

    run.__wrapped_for_context__ = True
    return run


__all__ = ["EngineContext", "bound", "binds_own_context", "current_context", "follow_process_config", "propagate",
           "state_context", "with_state_context"]
