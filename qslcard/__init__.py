"""QSL card aggregation and printing system.

Performance notes
-----------------
The core (model, ADIF, store, dedup, geometry, templates) is pure standard
library.  The only required third-party dependency is fpdf2, and it is
imported lazily, so importing qslcard stays cheap and the CLI/GUI start fast
even before a PDF is produced.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
