from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError

MAX_DOCUMENT_SIZE = 25 * 1024 * 1024

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

IMAGE_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF",
    ".webp": "WEBP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}


def validate_document_file(uploaded_file):
    filename = Path(uploaded_file.name).name
    extension = Path(filename).suffix.lower()
    if extension not in MIME_TYPES:
        raise ValidationError("Upload a PDF, DOCX, XLSX, or supported raster image.")
    if uploaded_file.size <= 0:
        raise ValidationError("The uploaded file is empty.")
    if uploaded_file.size > MAX_DOCUMENT_SIZE:
        raise ValidationError("Files must not exceed 25 MB.")

    uploaded_file.seek(0)
    header = uploaded_file.read(16)
    uploaded_file.seek(0)

    if extension == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise ValidationError("The file content is not a valid PDF.")
    elif extension in {".docx", ".xlsx"}:
        try:
            with ZipFile(uploaded_file) as archive:
                members = set(archive.namelist())
        except (BadZipFile, OSError):
            raise ValidationError("The file content is not a valid Office document.") from None
        required_members = (
            {"[Content_Types].xml", "word/document.xml"}
            if extension == ".docx"
            else {"[Content_Types].xml", "xl/workbook.xml"}
        )
        if not required_members.issubset(members):
            raise ValidationError("The file content does not match its Office document type.")
    else:
        expected_format = IMAGE_FORMATS[extension]
        try:
            with Image.open(uploaded_file) as image:
                if image.format != expected_format:
                    raise ValidationError("The image content does not match its file extension.")
                image.verify()
        except ValidationError:
            raise
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValidationError("The uploaded image is invalid or unsafe.") from None

    uploaded_file.seek(0)
    return MIME_TYPES[extension]
