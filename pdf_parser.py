import io
import re
import base64
import gc

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from PIL import Image
except ImportError:
    Image = None


def scale_ingredients_text(text, factor=1.0, *args, **kwargs):
    """Skaluje ilości składników w tekście o dany współczynnik."""
    if not text:
        return ""
    try:
        f = float(factor)
        if len(args) > 0 and args[0]:
            base = float(args[0])
            if base > 0:
                f = f / base

        if f == 1.0 or f <= 0:
            return text

        def replace_num(match):
            val = float(match.group(0).replace(',', '.'))
            scaled = val * f
            if scaled.is_integer():
                return str(int(scaled))
            return f"{scaled:.1f}".replace('.', ',')

        return re.sub(r'\b\d+(?:[.,]\d+)?\b', replace_num, str(text))
    except Exception:
        return str(text)


def compress_image_bytes(img_bytes, max_size=(350, 350), quality=45):
    """Kompresuje obraz do bardzo małych rozmiarów, oszczędzając pamięć RAM serwera."""
    if not Image:
        return None
    try:
        with Image.open(io.BytesIO(img_bytes)) as img:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return None


def parse_meal_text(text):
    """Wyciąga dane o posiłku z tekstu pojedynczej strony PDF."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    full_text = "\n".join(lines)

    # Numer posiłku i tytuł
    posilek_nr = 1
    posilek_match = re.search(r"Posiłek\s*(\d+)", full_text, re.IGNORECASE)
    if posilek_match:
        try:
            posilek_nr = int(posilek_match.group(1))
        except ValueError:
            posilek_nr = 1

    tytul = lines[0]
    for line in lines:
        if len(line) > 3 and not re.match(r"^(Posiłek|Kcal|Białko|Składniki|Sposób|Przygotowanie|\d+)", line, re.IGNORECASE):
            tytul = line
            break

    # Kalorie i makroskładniki
    kcal = 0
    bialko = 0.0
    wegle = 0.0
    tluszcze = 0.0

    kcal_m = re.search(r"(\d+)\s*kcal", full_text, re.IGNORECASE)
    if kcal_m:
        kcal = int(kcal_m.group(1))

    b_m = re.search(r"(?:Białko|B:)\s*(\d+(?:[.,]\d+)?)\s*g?", full_text, re.IGNORECASE)
    if b_m:
        bialko = float(b_m.group(1).replace(",", "."))

    w_m = re.search(r"(?:Węglowodany|W:)\s*(\d+(?:[.,]\d+)?)\s*g?", full_text, re.IGNORECASE)
    if w_m:
        wegle = float(w_m.group(1).replace(",", "."))

    t_m = re.search(r"(?:Tłuszcze|T:)\s*(\d+(?:[.,]\d+)?)\s*g?", full_text, re.IGNORECASE)
    if t_m:
        tluszcze = float(t_m.group(1).replace(",", "."))

    # Składniki i przygotowanie
    skladniki_list = []
    przygotowanie_list = []
    mode = None

    for line in lines:
        l_lower = line.lower()
        if "składniki" in l_lower:
            mode = "skladniki"
            continue
        elif "sposób przygotowania" in l_lower or "przygotowanie" in l_lower or "sposób wykonania" in l_lower:
            mode = "przygotowanie"
            continue
        elif re.match(r"^(posiłek\s*\d+|kcal|białko|węglowodany|tłuszcze)", l_lower):
            mode = None
            continue

        if mode == "skladniki":
            skladniki_list.append(line)
        elif mode == "przygotowanie":
            przygotowanie_list.append(line)

    skladniki_txt = "\n".join(skladniki_list) if skladniki_list else full_text
    przygotowanie_txt = "\n".join(przygotowanie_list) if przygotowanie_list else "Brak opisu przygotowania."

    return {
        "tytul": tytul,
        "posilek_nr": posilek_nr,
        "kcal": kcal,
        "bialko": bialko,
        "wegle": wegle,
        "tluszcze": tluszcze,
        "skladniki": skladniki_txt,
        "przygotowanie": przygotowanie_txt
    }


def extract_meals_and_images_from_pdf(pdf_file):
    """Główna funkcja czytająca PDF z zabezpieczeniem pamięci RAM."""
    if fitz is None:
        return []

    pdf_bytes = pdf_file.read()
    extracted_recipes = []

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []

    for page_num in range(len(doc)):
        try:
            page = doc[page_num]
            text = page.get_text("text")

            meal_data = parse_meal_text(text)
            if not meal_data:
                continue

            # Pobranie miniaturki obrazu (jeśli istnieje)
            image_url = None
            try:
                image_list = page.get_images(full=True)
                if image_list:
                    xref = image_list[0][0]
                    base_image = doc.extract_image(xref)
                    if base_image and "image" in base_image:
                        image_url = compress_image_bytes(base_image["image"])
            except Exception:
                image_url = None

            meal_data["image_url"] = image_url
            extracted_recipes.append(meal_data)

        except Exception:
            continue
        finally:
            gc.collect()

    doc.close()
    gc.collect()
    return extracted_recipes