"""Tag similarity: the cosine of the centroids of two tag sets' embeddings, mapped to 0..1.

Tag vectors live in a ``TagSpace``: the vectors a trained engine carries (``preloaded``: identical
behaviour anywhere) and those computed on demand by the embedding backend (sentence-transformers,
else a hashing vectorizer, else a character fallback), with their caches.

Every engine context (core_engine.context.EngineContext: a game, a training run) reads its own
space, so one engine's trained vectors never leak into another world: ``semantic_tag_similarity``
uses the bound context's space, else the process-wide default space (``load_tag_vectors``, the
older, process-wide API, loads into that one). Spaces with the same embedding model and the same
preloaded vectors behave identically, so they are shared (``shared_space``) and keep one cache.
"""
import collections
import contextlib
import hashlib
import io
import json
import threading

import numpy as np

from utils.config_loader import ACTIVE_CONTEXT, config

DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Embedding backends are imported only when a tag has to be encoded, so a run
# with pre-computed tag vectors (an offline engine export) needs neither
# sentence-transformers/torch nor scikit-learn.
_TAG_EMBEDDING_MODELS = {}  # model name -> SentenceTransformer, or None (not available)
# Loading the embedding model (sentence-transformers, torch) happens once, under this lock:
# two threads importing torch at once break the import, and one that finds the model
# "loading" must wait for it rather than fall back to other vectors.
_TAG_MODEL_LOCK = threading.Lock()
_TAG_HASHING_VECTORIZER = None
_TAG_HASHING_READY = False


def _hashing_vectorizer():
    global _TAG_HASHING_VECTORIZER, _TAG_HASHING_READY
    if _TAG_HASHING_READY:
        return _TAG_HASHING_VECTORIZER
    with _TAG_MODEL_LOCK:
        if not _TAG_HASHING_READY:
            try:
                from sklearn.feature_extraction.text import HashingVectorizer
                _TAG_HASHING_VECTORIZER = HashingVectorizer(analyzer="char_wb", ngram_range=(2, 5), n_features=256, norm=None)
            except Exception:  # not installed, or a broken install
                _TAG_HASHING_VECTORIZER = None
            _TAG_HASHING_READY = True
    return _TAG_HASHING_VECTORIZER


def _get_tag_embedding_model(model_name=None):
    model_name = model_name or DEFAULT_MODEL_NAME
    if model_name in _TAG_EMBEDDING_MODELS:
        return _TAG_EMBEDDING_MODELS[model_name]
    with _TAG_MODEL_LOCK:
        if model_name in _TAG_EMBEDDING_MODELS:  # another thread loaded it while this one waited
            return _TAG_EMBEDDING_MODELS[model_name]
        try:
            from sentence_transformers import SentenceTransformer

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                model = SentenceTransformer(model_name, local_files_only=True)
        except Exception:  # not installed, not cached, or a broken install: the fallback vectors
            model = None
        _TAG_EMBEDDING_MODELS[model_name] = model
    return model


def _normalize_tag_text(tag):
    return str(tag).replace("_", " ").strip().lower()


def _flatten_tags(tags):
    flattened = []
    for tag in tags or []:
        if isinstance(tag, (list, tuple, set, frozenset)):
            flattened.extend(_flatten_tags(tag))
        elif tag is not None:
            flattened.append(_normalize_tag_text(tag))
    return [tag for tag in flattened if tag]


def _normalized_tag_key(tags):
    return tuple(sorted(set(_flatten_tags(tags))))


def vectors_digest(vectors):
    """A digest of {tag: vector} (the same vectors: the same digest)."""
    if not vectors:
        return None
    payload = json.dumps({_normalize_tag_text(t): [float(x) for x in v] for t, v in vectors.items()},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


class TagSpace:
    """Tag vectors and the similarity caches computed from them.

    ``vectors`` ({tag: [floats]}): pre-computed vectors (those an engine was trained with); a tag
    they lack is then neutral (a zero vector of the same size). Without them every tag is encoded
    by the embedding backend (``model_name``: semantic_similarity.model_name, read when the first
    tag is encoded if None)."""

    def __init__(self, vectors=None, model_name=None):
        self._model_name = model_name
        self._lock = threading.Lock()
        self._preloaded = {}
        self._dimension = None
        self._embed = {}
        self._centroids = {}
        self._similarity = {}
        self._key = None
        if vectors:
            self.load(vectors)

    # ---- the vectors --------------------------------------------------------------------------

    def load(self, vectors):
        """Add pre-computed vectors ({tag: [floats]}); the caches computed so far are dropped."""
        with self._lock:
            for tag, vector in vectors.items():
                array = np.asarray(vector, dtype=float)
                self._preloaded[_normalize_tag_text(tag)] = array
            if vectors:
                self._dimension = len(next(iter(vectors.values())))
            self._embed = dict(self._preloaded)
            self._centroids.clear()
            self._similarity.clear()
            self._key = None

    def reset(self):
        """Forget the pre-computed vectors and every cache (tags are encoded afresh)."""
        with self._lock:
            self._preloaded.clear()
            self._dimension = None
            self._embed.clear()
            self._centroids.clear()
            self._similarity.clear()
            self._key = None

    @property
    def preloaded(self):
        """{tag: [floats]} of the pre-computed vectors (a copy)."""
        with self._lock:
            return {tag: vector.tolist() for tag, vector in self._preloaded.items()}

    @property
    def model_name(self):
        return self._model_name or config.get("semantic_similarity.model_name", DEFAULT_MODEL_NAME)

    @property
    def key(self):
        """What this space's similarities depend on: (embedding model, digest of the preloaded
        vectors). Two spaces with the same key give the same similarities."""
        if self._key is None:
            with self._lock:
                preloaded = {tag: vector.tolist() for tag, vector in self._preloaded.items()}
            self._key = (self.model_name, vectors_digest(preloaded))
        return self._key

    # ---- encoding -----------------------------------------------------------------------------

    def encode(self, tag_text):
        with self._lock:
            cached = self._embed.get(tag_text)
            if cached is not None:
                return cached
            dimension = self._dimension

        if dimension is not None:
            # A tag the engine was not trained with: neutral, same size as the others.
            vector = np.zeros(dimension)
        else:
            vector = _encode_with_backend(tag_text, self.model_name)
        with self._lock:
            return self._embed.setdefault(tag_text, vector)

    def export(self, tags):
        """{tag: vector} for the given tags, as this space encodes them."""
        return {text: self.encode(text).tolist() for text in sorted({_normalize_tag_text(t) for t in tags if t})}

    def _centroid(self, tag_key):
        with self._lock:
            cached = self._centroids.get(tag_key)
            if cached is not None:
                return cached
        embeddings = np.vstack([self.encode(tag) for tag in tag_key])
        centroid = embeddings.mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm
        with self._lock:
            self._centroids[tag_key] = centroid
        return centroid

    def similarity(self, tags1, tags2):
        normalized_1 = _normalized_tag_key(tags1)
        normalized_2 = _normalized_tag_key(tags2)
        if not normalized_1 or not normalized_2:
            return 0.0
        cache_key = (normalized_1, normalized_2) if normalized_1 <= normalized_2 else (normalized_2, normalized_1)
        with self._lock:
            cached = self._similarity.get(cache_key)
            if cached is not None:
                return cached

        centroid_1 = self._centroid(normalized_1)
        centroid_2 = self._centroid(normalized_2)
        if not np.any(centroid_1) or not np.any(centroid_2):
            similarity = 0.0
        else:
            raw_similarity = float(np.dot(centroid_1, centroid_2))
            similarity = max(0.0, min(1.0, (raw_similarity + 1.0) / 2.0))
        with self._lock:
            self._similarity[cache_key] = similarity
        return similarity


def _encode_with_backend(tag_text, model_name):
    model = _get_tag_embedding_model(model_name)
    if model is not None:
        return np.asarray(model.encode(tag_text, convert_to_numpy=True, normalize_embeddings=True), dtype=float)
    if _hashing_vectorizer() is not None:
        vector = _hashing_vectorizer().transform([tag_text]).toarray()[0].astype(float)
    else:
        vector = np.asarray([1.0 if i < len(tag_text) and tag_text[i].isalnum() else 0.0 for i in range(32)],
                            dtype=float)
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector /= norm
    return vector


# ---- the spaces --------------------------------------------------------------------------------

# The process-wide space: what code with no engine context bound reads (load_tag_vectors).
_DEFAULT_SPACE = TagSpace()
# Shared spaces by key (embedding model, preloaded vectors' digest), so the games of one engine share
# their caches; the least recently used are dropped (a context keeps its own alive).
_SHARED_SPACES = collections.OrderedDict()
_SHARED_LIMIT = 16
_SHARED_LOCK = threading.Lock()


def shared_space(vectors=None, model_name=None):
    """The shared TagSpace of these pre-computed ``vectors`` (None: none, every tag encoded) and this
    embedding model."""
    model_name = model_name or DEFAULT_MODEL_NAME
    key = (model_name, vectors_digest(vectors))
    with _SHARED_LOCK:
        space = _SHARED_SPACES.get(key)
        if space is not None:
            _SHARED_SPACES.move_to_end(key)
            return space
    space = TagSpace(vectors, model_name=model_name)
    with _SHARED_LOCK:
        space = _SHARED_SPACES.setdefault(key, space)
        _SHARED_SPACES.move_to_end(key)
        while len(_SHARED_SPACES) > _SHARED_LIMIT:
            _SHARED_SPACES.popitem(last=False)
    return space


def default_space():
    """The process-wide TagSpace (no engine context bound)."""
    return _DEFAULT_SPACE


def current_space():
    """The TagSpace of the bound engine context, else the process-wide one."""
    bound = ACTIVE_CONTEXT.get()
    space = getattr(bound, "tag_space", None) if bound is not None else None
    return space if space is not None else _DEFAULT_SPACE


# ---- the module API (unchanged) ------------------------------------------------------------------

def load_tag_vectors(vectors):
    """Use pre-computed tag vectors ({tag: [floats]}), e.g. those an engine was trained with, in the
    process-wide space (what runs with no engine context bound). Engine contexts keep their own."""
    _DEFAULT_SPACE.load(vectors)


def reset_tag_vectors():
    """Forget the process-wide pre-computed tag vectors (the files' configuration is in use again)."""
    _DEFAULT_SPACE.reset()


# An uploaded configuration or a trained engine's world set aside: its vectors go with it, so the
# files' world is not compared with another world's trained vectors.
config.on_override_cleared(reset_tag_vectors)


def export_tag_vectors(tags):
    """{tag: vector} for the given tags, computed exactly as in this environment (the bound engine
    context's space, else the process-wide one)."""
    return current_space().export(tags)


def _encode_single_tag(tag_text):
    return current_space().encode(tag_text)


def semantic_tag_similarity(tags1, tags2):
    return current_space().similarity(tags1, tags2)
