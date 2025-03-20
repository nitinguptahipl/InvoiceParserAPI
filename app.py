import os
import base64
import json
import requests
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from PyPDF2 import PdfReader, PdfWriter
from io import BytesIO
import asyncio
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

# Initialize Flask app
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'pdf', 'png', 'jpg', 'jpeg'}

# Create uploads folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Path to the JSON file (provided by Render)
json_file_path = '/etc/secrets/googleKey.json'

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

class InvoiceParserService:
    def __init__(self):
        self.project_id = "invoiceparser-452812"
        self.location = "us"  # Change based on your region
        self.processor_id = "73b3f94ebcf4188b"
        self.service_account_path = "assets/googleKey.json"  # Path to JSON key

    def _get_auth_token(self):
        try:
            # Load JSON from file
            with open(json_file_path, 'r') as f:
                service_account_json = json.load(f)
            
            credentials = Credentials.from_service_account_info(
                service_account_json,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )
            credentials.refresh(Request())
            return credentials.token
        except Exception as e:
            print(f"Error loading JSON key: {e}")
            raise

    async def parse_invoices(self, file_paths, multi_page=True):
        try:
            if not file_paths:
                return []

            auth_token = self._get_auth_token()  # Fetch auth token

            all_extracted_data = []

            for file_path in file_paths:
                try:
                    with open(file_path, 'rb') as f:
                        bytes_data = f.read()

                    file_extension = os.path.splitext(file_path)[1].lower()

                    if multi_page and file_extension == '.pdf':
                        # Handle multi-page PDF
                        pages = self._extract_pdf_pages(bytes_data)
                        for i, page in enumerate(pages):
                            extracted_data = await self._process_page(auth_token, page, 'application/pdf')
                            all_extracted_data.append({
                                "fileName": f"{os.path.basename(file_path)} - Page {i + 1}",
                                "data": extracted_data,
                            })
                    else:
                        # Determine MIME type
                        mime_type = 'application/pdf' if file_extension == '.pdf' else \
                                    'image/jpeg' if file_extension in ['.jpg', '.jpeg'] else \
                                    'image/png' if file_extension == '.png' else \
                                    'application/pdf'  # Default to PDF if unknown

                        # Handle single file or non-PDF
                        extracted_data = await self._process_page(auth_token, bytes_data, mime_type)
                        all_extracted_data.append({
                            "fileName": os.path.basename(file_path),
                            "data": extracted_data,
                        })
                except Exception as e:
                    print(f"Error processing file {file_path}: {e}")
                    all_extracted_data.append({
                        "fileName": os.path.basename(file_path),
                        "error": str(e),
                        "data": []
                    })

            return all_extracted_data
        except Exception as e:
            print(f"Error in parse_invoices: {e}")
            return []

    def _extract_pdf_pages(self, bytes_data):
        pages_bytes = []

        try:
            # Load the PDF document
            pdf_reader = PdfReader(BytesIO(bytes_data))
            for i in range(len(pdf_reader.pages)):
                try:
                    # Create a new PDF writer for each page
                    pdf_writer = PdfWriter()
                    pdf_writer.add_page(pdf_reader.pages[i])

                    # Save to bytes
                    output_stream = BytesIO()
                    pdf_writer.write(output_stream)
                    pages_bytes.append(output_stream.getvalue())

                    # Clean up
                    output_stream.close()
                except Exception as e:
                    print(f"Error processing page {i}: {e}")
        except Exception as e:
            print(f"Error extracting PDF pages: {e}")

        return pages_bytes

    async def _process_page(self, auth_token, page_bytes, mime_type):
        try:
            url = f"https://us-documentai.googleapis.com/v1/projects/{self.project_id}/locations/{self.location}/processors/{self.processor_id}:process"

            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {auth_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "rawDocument": {
                        "mimeType": mime_type,
                        "content": base64.b64encode(page_bytes).decode('utf-8'),
                    }
                },
            )

            if response.status_code == 200:
                return self._extract_invoice_data(response.json())
            else:
                print(f"Error processing document: {response.status_code} - {response.text}")
                return []
        except Exception as e:
            print(f"Error in API request: {e}")
            return []

    def _extract_invoice_data(self, response_json):
        try:
            document = response_json.get('document', {})
            entities = document.get('entities', [])

            if not entities:
                return []

            return [{
                "type": entity.get("type", "Unknown"),
                "value": entity.get("mentionText", ""),
            } for entity in entities]
        except Exception as e:
            print(f"Error extracting invoice data: {e}")
            return []

# Create API routes
@app.route('/parse-invoices', methods=['POST'])
def parse_invoices_api():
    # Check if files were uploaded
    if 'files' not in request.files:
        return jsonify({"error": "No files part in the request"}), 400
    
    files = request.files.getlist('files')
    
    if not files or files[0].filename == '':
        return jsonify({"error": "No files selected"}), 400
    
    # Get multi_page parameter (default to True)
    multi_page = request.form.get('multi_page', 'true').lower() == 'true'
    
    # Save uploaded files
    file_paths = []
    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            file_paths.append(file_path)
    
    if not file_paths:
        return jsonify({"error": "No valid files uploaded"}), 400
    
    # Parse invoices
    invoice_parser = InvoiceParserService()
    
    # Use asyncio to run the async parse_invoices method
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    extracted_data = loop.run_until_complete(invoice_parser.parse_invoices(file_paths, multi_page))
    loop.close()
    
    # Clean up uploaded files
    for file_path in file_paths:
        try:
            os.remove(file_path)
        except Exception as e:
            print(f"Error removing file {file_path}: {e}")
    
    return jsonify(extracted_data)

# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "service": "invoice-parser-api"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
