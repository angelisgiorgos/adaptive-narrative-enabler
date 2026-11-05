import contextlib
import io
import threading

import numpy as np

from utils.config_loader import config

# Embedding backends are imported only when a tag has to be encoded, so a run
# with pre-computed tag vectors (an offline engine export) needs neither
# sentence-transformers/torch nor scikit-learn.
_TAG_EMBEDDING_MODEL = None
_TAG_EMBEDDING_MODEL_READY = False
_TAG_EMBED_CACHE = {}
_TAG_SET_CENTROID_CACHE = {}
_TAG_SIMILARITY_CACHE = {}
_TAG_CACHE_LOCK = threading.Lock()
_TAG_HASHING_VECTORIZER = None
_TAG_HASHING_READY = False
_PRELOADED_DIMENSION = None  # set when pre-computed vectors are loaded


def _hashing_vectorizer():
    global _TAG_HASHING_VECTORIZER, _TAG_HASHING_READY
    if not _TAG_HASHING_READY:
        _TAG_HASHING_READY = True
        try:
            from sklearn.feature_extraction.text import HashingVectorizer
            _TAG_HASHING_VECTORIZER = HashingVectorizer(analyzer="char_wb", ngram_range=(2, 5), n_features=256, norm=None)
        except ImportError:
            _TAG_HASHING_VECTORIZER = None
    return _TAG_HASHING_VECTORIZER


def load_tag_vectors(vectors):
    """Use pre-computed tag vectors ({tag: [floats]}), e.g. those an engine was trained with."""
    global _PRELOADED_DIMENSION
    with _TAG_CACHE_LOCK:
        for tag, vector in vectors.items():
            _TAG_EMBED_CACHE[_normalize_tag_text(tag)] = np.asarray(vector, dtype=float)
        if vectors:
            _PRELOADED_DIMENSION = len(next(iter(vectors.values())))
        _TAG_SET_CENTROID_CACHE.clear()
        _TAG_SIMILARITY_CACHE.clear()


def export_tag_vectors(tags):
    """{tag: vector} for the given tags, computed exactly as in this environment."""
    return {text: _encode_single_tag(text).tolist() for text in sorted({_normalize_tag_text(t) for t in tags if t})}


def _normalize_tag_text(tag):
    return str(tag).replace("_", " ").strip().lower()


def _flatten_tags(tags):
    flattened = []
    for tag in tags or []:
        if isinstance(tag, (list, tuple, set)):
            flattened.extend(_flatten_tags(tag))
        elif tag is not None:
            flattened.append(_normalize_tag_text(tag))
    return [tag for tag in flattened if tag]


def _get_tag_embedding_model():
    global _TAG_EMBEDDING_MODEL, _TAG_EMBEDDING_MODEL_READY
    if _TAG_EMBEDDING_MODEL_READY:
        return _TAG_EMBEDDING_MODEL

    _TAG_EMBEDDING_MODEL_READY = True
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None

    model_name = config.get(
        "semantic_similarity.model_name",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            _TAG_EMBEDDING_MODEL = SentenceTransformer(model_name, local_files_only=True)
    except Exception:
        _TAG_EMBEDDING_MODEL = None
    return _TAG_EMBEDDING_MODEL


def _encode_single_tag(tag_text):
    with _TAG_CACHE_LOCK:
        cached = _TAG_EMBED_CACHE.get(tag_text)
        if cached is not None:
            return cached

    if _PRELOADED_DIMENSION is not None:
        # A tag the engine was not trained with: neutral, same size as the others.
        vector = np.zeros(_PRELOADED_DIMENSION)
        with _TAG_CACHE_LOCK:
            _TAG_EMBED_CACHE[tag_text] = vector
        return vector

    model = _get_tag_embedding_model()
    if model is not None:
        vector = np.asarray(
            model.encode(tag_text, convert_to_numpy=True, normalize_embeddings=True),
            dtype=float,
        )
    elif _hashing_vectorizer() is not None:
        vector = _hashing_vectorizer().transform([tag_text]).toarray()[0].astype(float)
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector /= norm
    else:
        vector = np.asarray(
            [1.0 if i < len(tag_text) and tag_text[i].isalnum() else 0.0 for i in range(32)],
            dtype=float,
        )
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector /= norm

    with _TAG_CACHE_LOCK:
        _TAG_EMBED_CACHE[tag_text] = vector
    return vector


def _normalized_tag_key(tags):
    return tuple(sorted(set(_flatten_tags(tags))))


def _centroid_for_tag_key(tag_key):
    with _TAG_CACHE_LOCK:
        cached = _TAG_SET_CENTROID_CACHE.get(tag_key)
        if cached is not None:
            return cached

    embeddings = np.vstack([_encode_single_tag(tag) for tag in tag_key])
    centroid = embeddings.mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid = centroid / norm

    with _TAG_CACHE_LOCK:
        _TAG_SET_CENTROID_CACHE[tag_key] = centroid
    return centroid


def semantic_tag_similarity(tags1, tags2):
    normalized_1 = _normalized_tag_key(tags1)
    normalized_2 = _normalized_tag_key(tags2)
    if not normalized_1 or not normalized_2:
        return 0.0

    cache_key = (
        normalized_1,
        normalized_2,
    ) if normalized_1 <= normalized_2 else (
        normalized_2,
        normalized_1,
    )

    with _TAG_CACHE_LOCK:
        cached = _TAG_SIMILARITY_CACHE.get(cache_key)
        if cached is not None:
            return cached

    centroid_1 = _centroid_for_tag_key(normalized_1)
    centroid_2 = _centroid_for_tag_key(normalized_2)

    if not np.any(centroid_1) or not np.any(centroid_2):
        similarity = 0.0
    else:
        raw_similarity = float(np.dot(centroid_1, centroid_2))
        similarity = max(0.0, min(1.0, (raw_similarity + 1.0) / 2.0))

    with _TAG_CACHE_LOCK:
        _TAG_SIMILARITY_CACHE[cache_key] = similarity
    return similarity
