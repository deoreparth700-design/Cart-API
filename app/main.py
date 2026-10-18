from fastapi import FastAPI

app = FastAPI(
    title="CartAPI",
    description="E-commerce orders and payments backend with Razorpay",
    version="1.0.0",
)


# Returns a simple JSON response indicating that the API service is up and running.
# Clients and uptime monitors use this endpoint to verify server health without needing authentication.
@app.get("/health")
def health_check():
    return {"status": "ok"}
