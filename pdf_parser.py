import io
import re
import base64
import gc
from PIL import Image

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


def scale_ingredients_text(text, factor):
    if factor == 1.0 or not text:
        return text

    def repl(match):
        val_str = match.group(1).replace(',', '.')
        unit = match.group(2)
        try:
            val = float(val_str) * factor
            val_formatted = f"{val:.1f}".rstrip('0').rstrip('.')
            return f"{val_formatted} {unit}"
        except Exception:
            return match.group(0)

    pattern = r'(\d+(?:[\.,]\d+)?)\s*(g|ml|szt|sztuka|sztuki|sztuk|plastry|plastra|plastru|łyżka|łyżki|łyżeczka|łyżeczki|szczypta|opakowania|opakowanie)'
    return re.sub(pattern, repl, text, flags=re.IGNORECASE)


def compress_image_bytes(img_bytes, max_size=(600, 600), quality=65):
    """Kompresuje obrazek i zmniejsza jego rozmiar, oszczędzając RAM i pamięć bazy."""
    try:
        with Image.open(io.BytesIO(img_bytes)) as img:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return None


def extract_meals_and_images_from_pdf(pdf_file):
    if fitz is None:
        return []

    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    extracted_recipes = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")

        # Pobieranie obrazu z danej strony z kompresją
        image_url = None
        image_list = page.get_images(full=True)
        if image_list:
            xref = image_list[0][0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            image_url = compress_image_bytes(image_bytes)

        # Tutaj następuje dalsza logika parsowania treści posiłku z `text`
        # ... (zachowaj dotychczasową logikę wyciągania tytułu, składników i kcal)

        # Wymuszenie czyszczenia pamięci po każdej stronie PDF
        gc.collect()

    doc.close()
    return extracted_recipes