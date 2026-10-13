"""AWS Lambda entry point (deploy/lambda/): the same FastAPI app, served
through a Lambda Function URL by Mangum, which turns each Function URL event
into an ASGI request and the response back. Nothing else changes -- local
runs and the container keep using uvicorn (entrypoint.sh)."""

from mangum import Mangum

from app import app

handler = Mangum(app, lifespan="off")
