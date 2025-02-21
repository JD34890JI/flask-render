import os
import shutil
import logging
from datetime import datetime
import pytesseract
from pdf2image import convert_from_path
import re
import zipfile
import time
import rarfile
import threading
from pathlib import Path
import json
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import io

app = Flask(__name__)
CORS(app)  # Esto permite las solicitudes CORS

class ActasRenamer:
    def __init__(self):
        pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
        
        self.work_dir = Path("actas_workspace")
        self.output_dir = self.work_dir / "actas_renombradas"
        self.setup_directories()
        
        # Cada elemento es una tupla: (ruta_archivo, zip_index)
        # Los PDFs directos tienen zip_index = None; los comprimidos se asignan según orden.
        self.files_to_process = []
        self.processed_count = 0
        self.success_count = 0
        self.total_files = 0
        
        # Este contador se asignará a cada ZIP de acuerdo a la ordenación que determinemos.
        self.zip_index_counter = 1
        
        self.is_processing = False
        self.setup_logging()

    def natural_sort_key_zip(self, s):
        """
        Genera una clave de ordenación para archivos ZIP/RAR.
        Se usa el nombre base sin la extensión y se intenta extraer
        el número entre paréntesis al final (por ejemplo, " (1)").
        Si existe, se usa ese número; de lo contrario se considera 0.
        La clave será una tupla (nombre_base, número).
        """
        base = os.path.splitext(os.path.basename(s))[0]
        # Buscar patrón como " (n)" al final del nombre
        m = re.search(r'\s*\((\d+)\)$', base)
        if m:
            num = int(m.group(1))
            base_clean = base[:m.start()].strip()
            return (base_clean.lower(), num)
        else:
            return (base.lower(), 0)
        
    def clean_workspace(self):
        for file in self.output_dir.glob('*'):
            try:
                if file.is_file():
                    os.remove(file)
                elif file.is_dir():
                    shutil.rmtree(file)
            except Exception as e:
                logging.error(f"Error al limpiar {file}: {e}")
        
        temp_dir = self.work_dir / "temp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        logging.info("Workspace limpiado exitosamente")
        
    def setup_directories(self):
        self.work_dir.mkdir(exist_ok=True)
        self.output_dir.mkdir(exist_ok=True)

    def extract_text_with_ocr(self, pdf_path):
        images = convert_from_path(pdf_path)
        text = pytesseract.image_to_string(images[0], lang='spa')
        logging.info(f"Texto extraído de {pdf_path}:\n{text}\n")
        return text

    def extract_store_name(self, text: str) -> str:
        patterns = [
            r"_(16\s+DE\s+SEPTIEMBRE)\s+LAP",
            r"_(16\s+DE\s+SEPTIEMBRE)\s+CEN",
            r"OXXO.*?_.*?_.*?_((?:[A-Z]+\s+)*[A-Z]+)\s+LAP",
            r"[O0][YXV][XK][O0Q].*?_.*?_.*?_((?:[A-Z]+\s+)*[A-Z]+)\s+LAP",
            r"Cliente:.*?OXXO.*?_((?:[A-Z]+\s+)*[A-Z]+)\s+LAP",
            r"OXXO.*?_.*?_.*?_((?:[A-Z]+\s+)*[A-Z]+)\s+CEN",
            r"[O0][YXV][XK][O0Q].*?_.*?_.*?_((?:[A-Z]+\s+)*[A-Z]+)\s+CEN",
            r"Cliente:.*?OXXO.*?_((?:[A-Z]+\s+)*[A-Z]+)\s+CEN",
            r"_(CATEDRAL\s+LA\s+PAZ)\s+LAP",
            r"_(PENINSULA\s+SUR)\s+LAP",
            r"_(VICENTE\s+GUERRERO)\s+LAP",
            r"_(ALTAVELA)\s+LAP",
            r"_(TENOCHTITLAN)\s+CEN",
            r"_([^_]+?)(?=\s+LAP)",
            r"_([A-Z][A-Za-z\s]+?)(?=\s+LAP)",
            r"_([^_]+?)(?=\s+CEN)",
            r"_([A-Z][A-Za-z\s]+?)(?=\s+CEN)",
            r"[O0][YXV][XK][O0Q].*?_.*?_.*?_([^_\n]+?)\s+LAP",
            r"Cliente:.*?_([^_\n]+?)\s+LAP",
            r"[O0][YXV][XK][O0Q].*?_.*?_.*?_([^_\n]+?)\s+CEN",
            r"Cliente:.*?_([^_\n]+?)\s+CEN"
        ]
        
        for pattern in patterns:
            if match := re.search(pattern, text, re.IGNORECASE):
                store_name = match.group(1).strip()
                logging.info(f"Nombre de tienda encontrado: {store_name}")
                if ' ' in store_name:
                    parts = store_name.split()
                    if len(parts) > 1 and all(len(part) >= 2 for part in parts):
                        return store_name
                return store_name
        logging.warning("No se encontró nombre de tienda en el texto")
        return None

    def clean_store_name(self, store_name: str) -> str:
        original_name = store_name
        store_name = re.sub(r'\d+[A-Z0-9]+\s+', '', store_name)
        if original_name != store_name:
            logging.info(f"Nombre limpiado: {original_name} -> {store_name}")
        return store_name.strip()
        
    def process_files(self, files):
        self.clean_workspace()
        self.files_to_process = []
        self.processed_count = 0
        self.success_count = 0
        self.zip_index_counter = 1
        self.is_processing = False
        
        # Separar PDFs directos y archivos comprimidos
        pdf_files = [f for f in files if f.lower().endswith('.pdf')]
        zip_files = [f for f in files if f.lower().endswith(('.zip', '.rar'))]
        
        imported_files = []
        
        for file in pdf_files:
            self.files_to_process.append((file, None))
            imported_files.append({"name": os.path.basename(file), "type": "pdf"})
            
        # Ordenamos los archivos comprimidos usando la clave natural
        zip_files_sorted = sorted(zip_files, key=self.natural_sort_key_zip)
        for file in zip_files_sorted:
            extracted_pdfs = self.extract_compressed(file, self.zip_index_counter)
            imported_files.append({
                "name": os.path.basename(file), 
                "type": "zip", 
                "pdfs": extracted_pdfs
            })
            self.zip_index_counter += 1
                
        self.total_files = len(self.files_to_process)
        return imported_files
        
    def extract_compressed(self, file_path, zip_index):
        temp_dir = self.work_dir / "temp" / f"zip_{zip_index}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        extracted_pdfs = []
        
        try:
            if file_path.lower().endswith('.zip'):
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
            else:
                with rarfile.RarFile(file_path, 'r') as rar_ref:
                    rar_ref.extractall(temp_dir)
            
            time.sleep(1)
            
            for pdf in sorted(temp_dir.rglob("*.pdf")):
                if pdf.exists() and os.access(pdf, os.R_OK):
                    self.files_to_process.append((str(pdf), zip_index))
                    extracted_pdfs.append(pdf.name)
                    
            return extracted_pdfs
                    
        except Exception as e:
            logging.error(f"Error al extraer {file_path}: {str(e)}")
            return []
    
    def start_processing(self):
        if self.is_processing:
            return False
            
        if not self.files_to_process:
            return False
            
        self.processed_count = 0
        self.success_count = 0
        self.is_processing = True
        
        # Iniciar procesamiento en un hilo separado
        threading.Thread(target=self._process_files_thread, daemon=True).start()
        return True

    def _process_files_thread(self):
        for file_info in self.files_to_process:
            file_path, zip_index = file_info
            try:
                success = self.process_single_file(file_path, zip_index)
                if success:
                    self.success_count += 1
            except Exception as e:
                logging.error(f"Error procesando {file_path}: {str(e)}")
                
            self.processed_count += 1
            time.sleep(0.2)  # Pequeña pausa para no saturar el sistema
        
        self.is_processing = False
        
    def process_single_file(self, file_path, zip_index):
        original_filename = os.path.basename(file_path)
        try:
            text = self.extract_text_with_ocr(file_path)
            store_name = self.extract_store_name(text)
            if store_name:
                store_name = self.clean_store_name(store_name)
                new_filename = original_filename.replace('.pdf', f' {store_name}.pdf')
            else:
                logging.error(f"No se pudo encontrar nombre de tienda en: {file_path}")
                new_filename = original_filename
        except Exception as e:
            logging.error(f"Error procesando {file_path}: {str(e)}")
            new_filename = original_filename
        
        if zip_index is not None:
            target_dir = self.output_dir / str(zip_index)
            target_dir.mkdir(exist_ok=True)
        else:
            target_dir = self.output_dir
        
        new_path = os.path.join(target_dir, new_filename)
        
        try:
            shutil.copy2(file_path, new_path)
            logging.info(f"Archivo procesado: {original_filename} -> {new_filename} en {target_dir}")
        except Exception as e:
            logging.error(f"Error copiando {file_path} a {new_path}: {e}")
        
        return True
    
    def get_progress(self):
        return {
            "processed": self.processed_count,
            "total": self.total_files,
            "success": self.success_count,
            "is_processing": self.is_processing,
            "progress_percentage": int((self.processed_count / self.total_files * 100) if self.total_files > 0 else 0)
        }
    
    def create_zip_archive(self):
        if not self.output_dir.exists() or not any(self.output_dir.iterdir()):
            return None
            
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path in self.output_dir.rglob('*'):
                if file_path.is_file():
                    relative_path = file_path.relative_to(self.output_dir)
                    zf.write(file_path, arcname=relative_path)
        
        memory_file.seek(0)
        return memory_file
        
    def setup_logging(self):
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = log_dir / f'renombrar_actas_{timestamp}.log'
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )


# Inicializar el renombrador
renamer = ActasRenamer()

@app.route('/import', methods=['POST'])
def import_files():
    if 'files' not in request.files:
        return jsonify({"error": "No se recibieron archivos"}), 400
    
    uploaded_files = request.files.getlist('files')
    
    if not uploaded_files:
        return jsonify({"error": "No hay archivos seleccionados"}), 400
    
    # Guardar archivos temporalmente
    temp_paths = []
    for file in uploaded_files:
        temp_path = os.path.join(renamer.work_dir, file.filename)
        file.save(temp_path)
        temp_paths.append(temp_path)
    
    # Procesar los archivos
    imported_files = renamer.process_files(temp_paths)
    
    return jsonify({
        "status": "success",
        "imported_files": imported_files,
        "total_files": renamer.total_files
    })

@app.route('/start', methods=['POST'])
def start_processing():
    if renamer.start_processing():
        return jsonify({"status": "started"})
    else:
        return jsonify({"error": "No hay archivos para procesar o ya se está procesando"}), 400

@app.route('/progress', methods=['GET'])
def get_progress():
    return jsonify(renamer.get_progress())

@app.route('/download', methods=['GET'])
def download_results():
    if renamer.is_processing:
        return jsonify({"error": "El procesamiento aún está en curso"}), 400
    
    zip_buffer = renamer.create_zip_archive()
    if not zip_buffer:
        return jsonify({"error": "No hay archivos para descargar"}), 404
    
    return Response(
        zip_buffer.getvalue(),
        mimetype='application/zip',
        headers={
            'Content-Disposition': 'attachment; filename=actas_renombradas.zip'
        }
    )

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)