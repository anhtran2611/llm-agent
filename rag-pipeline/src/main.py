import os
import shutil
import time
import threading
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Response
from pydantic import BaseModel
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from data_pipeline import setup_pipeline
from model_setup import load_model
from utils import (get_model_dir, trace, tracer, logger, 
                   MODEL_LOAD_TIME, REQUEST_COUNT, LATENCY, 
                   monitor_memory_usage, secure_filename)

class ChatRequest(BaseModel):
    messages: str
    
class ModelState:
    def __init__(self):
        """State holder for the local LLM
        """
        self.llm_loaded = False
        self.qa_pipelines = {}
        self.model = None
        
# LLM global variables
model_state = ModelState()

# Define paths
UPLOAD_DIR = Path("./uploaded_pdfs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


app = FastAPI()
FastAPIInstrumentor.instrument_app(app)

def load_llm(model_name="Qwen/Qwen2.5-0.5B-Instruct"):
    """Function to load LLM on startup.

    Args:
        model_name (str, optional): Model name on [Hugging Face](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct). 
        Defaults to "Qwen/Qwen2.5-0.5B-Instruct".
    """
    with tracer.start_as_current_span("load_llm") as load_llm:
        start_time = time.time()
        
        try:
            # Loading LLM Model
            logger.info("🔄 Loading LLM ...")
            local_dir = get_model_dir(model_name)
            model_state.model = load_model(model_name=model_name, local_dir=local_dir)           
            MODEL_LOAD_TIME.observe(time.time() - start_time)
            model_state.llm_loaded = True
            logger.info("✅ LLM Model Loaded Successfully")
        except Exception as e:
            logger.error(f"❌ LLM Model Load Failed: {e}", exc_info=True)
            model_state.llm_loaded = False

@app.get("/metadata")
def get_metadata():
    return {"my_metadata": "This is a metadata endpoint."}

@app.get("/health")
async def health_check(response: Response):
    health_status = {"status": "healthy"}
    
    if not model_state.llm_loaded:
        response.status_code = 503
        health_status["status"] = "unhealthy"
        
    return health_status

@app.post("/api/upload_pdf", description="API endpoint to upload PDF documents.")
async def upload_pdf(user_id: str, file: UploadFile = File(...)):
    """Handle PDF upload, extracts text, and updates the vector database.

    Args:
        user_id (str): User ID for storing and retrieving PDF documents.
        file (UploadFile, optional): PDF file to be uploaded. Defaults to File(...).
    """
    with tracer.start_as_current_span("upload_pdf") as upload_pdf:
        if not model_state.llm_loaded:
            raise HTTPException(status_code=503, detail="LLM is still loading. Please wait.")
        try:
            # Save file locally
            file_name = secure_filename(file.filename)
            file_path = UPLOAD_DIR / f"{user_id}_{file_name}"
            os.makedirs("./uploaded_pdfs", exist_ok=True)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            # Update qa_pipeline with the new document
            logger.info(f" Updating retriever for user {user_id}...")
            with tracer.start_as_current_span(
                "setup_pipeline", links=[trace.Link(upload_pdf.get_span_context())]
            ):
            
            # Call to function setup_pipeline of data_pipeline.py
                model_state.qa_pipelines[user_id] = setup_pipeline(local_dir=get_model_dir(), file_path=str(file_path), model=model_state.model)
            logger.info("Retriever updated!")
            return {"message": "PDF processed and stored successfully", "file_path": file_path}

        except Exception as e:
            logger.error(f"❌ Error updating retriever: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/chat", description="API endpoint to chat with local LLM and measure latency with Prometheus.")
def chat_endpoint(user_id: str, request: ChatRequest):
    """Chat with the local LLM.

    Args:
        user_id (str): User ID.
        request (ChatRequest): Prompt to chat with the LLM.

    Returns:
        Response (json): {"response": answer of the LLM}.
    """
    REQUEST_COUNT.inc()
    start_time = time.time()
    
    if not model_state.llm_loaded:
        raise HTTPException(status_code=503, detail="LLM is still loading. Please wait.")
    
    user_pdfs = sorted(UPLOAD_DIR.glob(f"{user_id}_*.pdf"), key=os.path.getmtime, reverse=True)
    if not user_pdfs: 
        raise HTTPException(400, "No PDF found for this user. Upload a PDF first.")
    latest_pdf = str(user_pdfs[0])
    logger.info(f"Processing chat request using PDF: {latest_pdf}")
    
    # Get user-specific pipeline
    qa_pipeline = model_state.qa_pipelines.get(user_id)
    
    # Ensure retriever is ready
    if qa_pipeline is None:
        logger.info(f"QA pipeline is None")
        raise HTTPException(status_code=400, detail="QA pipeline is not ready. Upload PDF first")
    if model_state.model is None:
            model_state.model = load_model(
                model_name="Qwen/Qwen2.5-0.5B-Instruct", 
                local_dir=get_model_dir()
            )
    try:
        logger.info(f"QA pipeline invoke ...")
        response = qa_pipeline.invoke(request.messages)
        response_text = response["result"].split("Answer:")[-1].strip()
    except Exception as e:
        logger.error(f"Pipeline error: {str(e)}")
        raise HTTPException(500, "Failed to process request")
    
    LATENCY.observe(time.time() - start_time)
    return {"response": response_text}
    
@app.get("/api/config")
def get_config():
    return {
        "backend_name": "rag-pipeline",
        "models": [{"id": "qwen", "name": "Qwen 2.5 Instruct"}],
    }
    
@app.get("/metrics", description="Prometheus client's metrics.")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

if __name__ == "__main__":
    import uvicorn
    import argparse
    
    parser = argparse.ArgumentParser(description="Main script for running local LLM with FastAPI.")
    parser.add_argument('--port', type=int, default=8000,
                        help="Port for FastAPI"
                        )
    parser.add_argument('--model', type=str, default='Qwen/Qwen2.5-0.5B-Instruct',
                        help="Model name to download.")
    args = parser.parse_args()
    
    # Load local LLM
    load_llm(args.model)
    
    # Start memory monitoring
    threading.Thread(target=monitor_memory_usage, daemon=True).start()
    
    # Start FastAPI server
    uvicorn.run(app, host="0.0.0.0", port=args.port)