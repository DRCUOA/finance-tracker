"""Background jobs registered against the APScheduler instance owned by
:mod:`app.main`. Each job module exposes a top-level callable that opens its
own DB session — APScheduler invokes them outside any FastAPI request, so
``Depends(get_db)`` is not available."""
