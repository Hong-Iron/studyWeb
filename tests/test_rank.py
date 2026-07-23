"""BM25 ranking, tokenisation (en/ko), and extractive-answer edge cases."""

from studyweb.rank import (tokenize, BM25, rank_passages, extractive_answer,
                           normalize_scores, Passage)


def test_tokenize_en_ko_and_stopwords():
    toks = tokenize("The quick brown fox and 광합성 과정은 무엇인가")
    assert "quick" in toks and "brown" in toks
    assert "the" not in toks and "and" not in toks   # english stopwords
    assert any("광합성" in t for t in toks)            # korean kept


def test_bm25_ranks_relevant_higher():
    docs = [tokenize("cats are wonderful feline pets"),
            tokenize("the stock market rose today"),
            tokenize("felines and cats love to nap")]
    bm = BM25(docs)
    ranked = bm.rank("cats feline")
    assert ranked[0][0] in (0, 2)         # a cat doc ranks first
    assert ranked[-1][0] == 1             # finance doc ranks last


def test_normalize_scores():
    assert normalize_scores([]) == []
    assert normalize_scores([0, 0]) == [0.0, 0.0]
    ns = normalize_scores([2.0, 1.0])
    assert ns[0] == 1.0 and 0 < ns[1] < 1


def test_rank_passages_empty_and_scored():
    assert rank_passages("q", []) == []
    ps = [Passage("cats are great pets", "u1"),
          Passage("economics and inflation", "u2")]
    out = rank_passages("cats pets", ps, top_k=2)
    assert out[0][0].text.startswith("cats")
    assert 0 <= out[0][1] <= 1


def test_extractive_answer_rejects_citation_junk():
    junk = Passage('1 2 3 4 5 6 "Title A". "Title B". "Title C". "Title D".',
                   "u1")
    good = Passage("Photosynthesis is the process by which plants convert light "
                   "energy into chemical energy for growth.", "u2")
    ans = extractive_answer("photosynthesis process", [junk, good])
    assert "Photosynthesis is the process" in ans
    assert "1 2 3 4 5 6" not in ans


def test_extractive_answer_empty():
    assert extractive_answer("q", []) == ""
    # passages with no prose yield empty
    assert extractive_answer("q", [Passage("| | | 1 2 3", "u")]) == ""
