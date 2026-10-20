"""Text/audio alignment: EPUB text extraction, transcription, matching, sentence maps.

Pipeline (``lectern.align.pipeline.align_work``):

1. ``text``: walk the EPUB spine into one normalised text stream per item (the frozen
   normalisation from ``lectern.positions``) with a source map, segment it into sentences.
2. ``transcribe``: faster-whisper with word timestamps and VAD, every track on one timeline.
3. ``tokens``: the same normalisation (case, punctuation, numbers to words) on both streams.
4. ``matcher``: unique n-gram anchors, longest in-order chain, edit-distance between anchors,
   sentence first/last word times, interpolation of unmatched sentences, coverage and
   abridgment statistics.

Only ``transcribe`` needs the optional ``align`` extra (faster-whisper); everything else is
pure Python and runs on the server and in the tests.
"""
