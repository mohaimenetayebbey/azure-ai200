"""AI-200 document API: Blob Storage -> Service Bus -> Cosmos DB."""
import logging
import os
import uuid
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions
from azure.identity import DefaultAzureCredential
from azure.monitor.opentelemetry import configure_azure_monitor
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.storage.blob import BlobServiceClient, ContentSettings
from fastapi import FastAPI, File, HTTPException, UploadFile
from opentelemetry import trace

if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    configure_azure_monitor()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai200.api")
tracer = trace.get_tracer(__name__)
app = FastAPI(title="AI-200 Document Processing API", version="1.0.0")

credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT"]
BLOB_CONTAINER = os.getenv("BLOB_CONTAINER", "documents")
SERVICEBUS_NAMESPACE = os.environ["SERVICEBUS_NAMESPACE"]
SERVICEBUS_QUEUE = os.getenv("SERVICEBUS_QUEUE", "document-processing")
COSMOS_ENDPOINT = os.environ["COSMOS_ENDPOINT"]
COSMOS_DATABASE = os.getenv("COSMOS_DATABASE", "documentsdb")
COSMOS_CONTAINER = os.getenv("COSMOS_CONTAINER", "results")

blob_service = BlobServiceClient(
    f"https://{STORAGE_ACCOUNT}.blob.core.windows.net", credential=credential
)
servicebus = ServiceBusClient(SERVICEBUS_NAMESPACE, credential=credential)
cosmos = CosmosClient(COSMOS_ENDPOINT, credential=credential)
results = cosmos.get_database_client(COSMOS_DATABASE).get_container_client(COSMOS_CONTAINER)
MAX_UPLOAD_BYTES = 1024 * 1024


@app.get("/health")
def health():
    """Liveness only; does not assert dependency connectivity."""
    return {"status": "healthy", "service": "document-api"}


@app.get("/")
def root():
    return {"service": "AI-200 Document Processing API", "docs": "/docs"}


@app.post("/documents", status_code=202)
async def upload_document(file: UploadFile = File(...)):
    """Accept UTF-8 .txt documents and queue them for asynchronous AI processing."""
    if not (file.filename or "").lower().endswith(".txt"):
        raise HTTPException(415, "Only UTF-8 .txt files are supported in this lab")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File must contain 1 byte to 1 MiB")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Document must be UTF-8 text")

    document_id = str(uuid.uuid4())
    blob_name = f"{document_id}.txt"
    item = {
        "id": document_id,
        "documentId": document_id,  # Cosmos partition key
        "filename": (file.filename or "document.txt")[:200],
        "blobName": blob_name,
        "status": "queued",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with tracer.start_as_current_span("submit_document") as span:
            span.set_attribute("document.id", document_id)
            blob_service.get_blob_client(BLOB_CONTAINER, blob_name).upload_blob(
                raw, overwrite=False, content_settings=ContentSettings(content_type="text/plain")
            )
            results.create_item(item)
            with servicebus.get_queue_sender(queue_name=SERVICEBUS_QUEUE) as sender:
                sender.send_messages(ServiceBusMessage(
                    document_id, message_id=document_id,
                    application_properties={"documentId": document_id},
                ))
        logger.info("Queued document %s", document_id)
        return {"documentId": document_id, "status": "queued", "statusUrl": f"/documents/{document_id}"}
    except Exception:
        logger.exception("Unable to queue document %s", document_id)
        # Keep the record if it exists so operators can diagnose a partial submission.
        try:
            item["status"] = "submission_failed"
            results.upsert_item(item)
        except Exception:
            logger.exception("Could not record submission failure")
        raise HTTPException(503, "Unable to submit document; see application telemetry")


@app.get("/documents/{document_id}")
def get_document(document_id: str):
    try:
        return results.read_item(item=document_id, partition_key=document_id)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        raise HTTPException(404, "Document not found")
    except Exception:
        logger.exception("Unable to retrieve document %s", document_id)
        raise HTTPException(503, "Document status temporarily unavailable")
