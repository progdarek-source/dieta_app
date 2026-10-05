import re, base64
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# Logika skopiowana 1:1 z app.py (bez zmian)
# --- PRZELICZANIE GRAMATURY W TEKŚCIE SKŁADNIKÓW ---
def scale_ingredients_text(text, factor):
    """Automatycznie przelicza gramaturę w tekście składników (np. 150 g -> 300 g)."""
    if factor == 1.0 or not text:
        return text
    
    def repl(match):
        val_str = match.group(1).replace(',', '.')
        unit = match.group(2)
        try:
            val = float(val_str) * factor
            val_formatted = f"{val:.1f}".rstrip('0').rstrip('.')
            return f"{val_formatted} {unit}"
        except:
            return match.group(0)
            
    pattern = r'(\d+(?:[\.,]\d+)?)\s*(g|ml|szt|sztuka|sztuki|sztuk|plastry|plastra|plastru|łyżka|łyżki|łyżeczka|łyżeczki|szczypta|opakowania|opakowanie|szklanka|szklanki|kromka|kromki|ząbek|ząbki)\b'
    return re.sub(pattern, repl, text, flags=re.IGNORECASE)

# --- PARSER PDF ---
def extract_meals_and_images_from_pdf(pdf_file):
    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    extracted_recipes = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")

        if not re.search(r'Posiłek\s+\d+', text, re.I) and not re.search(r'\d+\s*Kcal', text, re.I):
            continue

        image_base64 = ""
        images = page.get_images(full=True)
        best_img_bytes = None
        best_xref = best_smask = 0
        max_size = 0
        ext = "png"
        
        for img_info in images:
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            if len(img_bytes) > max_size and len(img_bytes) > 5000:
                max_size = len(img_bytes)
                best_img_bytes = img_bytes
                ext = base_image["ext"]
                best_xref, best_smask = xref, img_info[1]

        if best_img_bytes and best_smask:  # ZMIANA: dołącz maskę przezroczystości (inaczej tło jest czarne)
            try:
                pix = fitz.Pixmap(doc, best_xref)
                if pix.colorspace and pix.colorspace.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pix = fitz.Pixmap(pix, fitz.Pixmap(doc, best_smask))
                best_img_bytes, ext = pix.tobytes("png"), "png"
            except Exception:
                pass
        if best_img_bytes:
            encoded = base64.b64encode(best_img_bytes).decode('utf-8')
            image_base64 = f"data:image/{ext};base64,{encoded}"

        nr_m = re.search(r'Posiłek\s+(\d+)', text, re.I)  # ZMIANA: numer posiłku 1-5
        kcal_m = re.search(r'(\d+)\s*Kcal', text, re.I)
        b_m = re.search(r'(\d+(?:[\.,]\d+)?)\s*g?\s*B\b', text, re.I)
        w_m = re.search(r'(\d+(?:[\.,]\d+)?)\s*g?\s*W\b', text, re.I)
        t_m = re.search(r'(\d+(?:[\.,]\d+)?)\s*g?\s*T\b', text, re.I)

        kcal = int(kcal_m.group(1)) if kcal_m else 500
        b = float(b_m.group(1).replace(',', '.')) if b_m else 20.0
        w = float(w_m.group(1).replace(',', '.')) if w_m else 50.0
        t = float(t_m.group(1).replace(',', '.')) if t_m else 15.0

        raw_lines = [l.strip() for l in text.split('\n') if l.strip()]
        cleaned_lines = []

        for l in raw_lines:
            if re.search(r'błonnik|wapń|magnez|Respo:|Plan diety|Dzień\s*\d+', l, re.I):
                continue
            if re.search(r'^Posiłek\s*\d+(\s*/\s*\d{1,2}:\d{2}-\d{1,2}:\d{2})?$', l, re.I):
                continue
            if re.match(r'^\d{1,2}:\d{2}-\d{1,2}:\d{2}$', l):
                continue
            if re.search(r'Sposób przygotowania', l, re.I):
                continue
            if re.search(r'^\d+[\.,]?\s*(Kcal|g\s*[BWT]|g|mg)?$', l, re.I) or l.lower() in ['kcal', 'b', 'w', 't']:
                continue
            cleaned_lines.append(l)

        title_lines, ingredients, raw_steps_lines = [], [], []

        def is_ingredient(line_str):
            pattern = r'–|-|\(\d+\s*g\)\b|\b\d+(?:[\.,]\d+)?\s*(g|ml|szt|sztuka|sztuki|sztuk|plastry|plastra|plastru|łyżka|łyżki|łyżeczka|łyżeczki|szczypta|opakowania|opakowanie|szklanka|szklanki|kromka|kromki|ząbek|ząbki)\b'
            return bool(re.search(pattern, line_str, re.I))

        mode = 'title'
        for l in cleaned_lines:
            if mode == 'title':
                if is_ingredient(l) or re.match(r'^\d+[\.\)]\s*', l):
                    mode = 'content'
                else:
                    title_lines.append(l)
                    continue

            if mode == 'content':
                if is_ingredient(l) and not re.match(r'^\d+[\.\)]\s*[A-ZĄĆĘŁŃÓŚŹŻ]', l):
                    ingredients.append(l)
                else:
                    raw_steps_lines.append(l)

        tytul = " ".join(title_lines).strip()
        tytul = re.sub(r'^\d+\s*/\s*\d{1,2}:\d{2}-\d{1,2}:\d{2}\s*', '', tytul)
        tytul = re.sub(r'\s+', ' ', tytul)
        tytul = re.sub(r'^Posiłek\s*\d+\s*(/\s*(dowolna pora dnia|\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}))?\s*', '', tytul, flags=re.I).strip() or tytul  # ZMIANA
        if not tytul:
            tytul = f"Przepis ze strony {page_num + 1}"

        merged_steps = []
        current_step = ""

        for line in raw_steps_lines:
            clean_l = re.sub(r'^\d+[\.\)]\s*', '', line).strip()
            if not clean_l:
                continue

            if current_step and (line[0].isupper() or re.match(r'^\d+[\.\)]', line)):
                if current_step.endswith(('.', '!', '?', ':')) or re.match(r'^\d+[\.\)]', line):
                    merged_steps.append(current_step)
                    current_step = clean_l
                else:
                    current_step += " " + clean_l
            else:
                if current_step:
                    current_step += " " + clean_l
                else:
                    current_step = clean_l

        if current_step:
            merged_steps.append(current_step)

        skladniki_txt = "\n".join(ingredients)
        przygotowanie_txt = "\n".join([f"{idx}. {step}" for idx, step in enumerate(merged_steps, 1)])

        extracted_recipes.append({
            "posilek_nr": int(nr_m.group(1)) if nr_m else 0,
            "tytul": tytul,
            "skladniki": skladniki_txt,
            "przygotowanie": przygotowanie_txt,
            "kcal": kcal,
            "bialko": b,
            "wegle": w,
            "tluszcze": t,
            "image_url": image_base64
        })

    return extracted_recipes

