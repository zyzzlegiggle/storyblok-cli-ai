from fastapi import FastAPI
from .api.generate import router as generate_router

app = FastAPI(title="Storyblok AI Backend")
app.include_router(generate_router, prefix="/generate")