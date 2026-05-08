"""Phase 5 (Polymarket prediction-market bot) package.

Recommend-only by design: this package contains the
data-and-decision layer (scanner, sizer, recommendation writer).
Order placement is the deliberate Phase-5 no-execute boundary
per architecture.md D.3 closing paragraph and is NOT implemented
here. Execution is the human's job after reviewing the
recommendation packet.
"""
