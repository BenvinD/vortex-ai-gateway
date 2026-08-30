"""Core gateway implementation."""

from fastapi import FastAPI

app = FastAPI(title="Vortex AI Gateway", version="0.1.0")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}
