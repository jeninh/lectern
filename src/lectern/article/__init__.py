"""Save a web article as an EPUB (issue #67): fetch, extract, build, file (docs/save-articles.md).

- :mod:`lectern.article.fetch`: a bounded HTTP GET that reaches only public internet addresses;
- :mod:`lectern.article.build`: the readable article from a page, and the EPUB built from it;
- :mod:`lectern.article.jobs`: the server-only ``article`` job that does both and files the
  result as a work.
"""

ARTICLE = "article"  # the job kind (lectern.article.jobs registers it)
