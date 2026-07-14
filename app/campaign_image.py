"""
Image insérée dans le corps d'une campagne (Option 3, onglet Configuration).

Sécurité : le fichier envoyé par l'utilisateur n'est JAMAIS stocké tel quel.
Il est décodé pixel par pixel puis intégralement ré-encodé avant écriture en
base — un fichier déguisé en image (payload caché dans les métadonnées ou
après les données d'image réelles) ne survit pas à ce ré-encodage, puisque
seuls les pixels décodés sont conservés. Ça supprime aussi les métadonnées
EXIF au passage (confidentialité : géolocalisation d'un téléphone, etc.).

Le type réel du fichier est vérifié via Pillow (signature binaire), jamais
via l'extension ni le Content-Type déclaré par le navigateur — les deux sont
falsifiables.

Poids final limité à 200 Ko, obtenu par compression JPEG progressive puis,
si nécessaire, réduction des dimensions.
"""
from io import BytesIO

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 200 * 1024
# Rejette les dimensions absurdes avant tout traitement lourd, pour éviter
# les attaques par décompression (fichier minuscule qui explose en mémoire).
MAX_DIMENSION = 4000
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
MIN_JPEG_QUALITY = 30


class ImageError(Exception):
    pass


def _open_and_validate(file_bytes):
    if not file_bytes:
        raise ImageError("Fichier image vide.")

    try:
        probe = Image.open(BytesIO(file_bytes))
        probe.verify()  # détecte un fichier corrompu ou non-image tôt, sans tout décoder
    except (UnidentifiedImageError, Exception) as exc:
        raise ImageError("Fichier non reconnu comme une image valide.") from exc

    # verify() invalide l'objet pour un usage ultérieur : on rouvre proprement.
    img = Image.open(BytesIO(file_bytes))
    if img.format not in ALLOWED_FORMATS:
        raise ImageError(
            f"Format d'image non supporté ({img.format or 'inconnu'}). "
            f"Utilisez JPEG, PNG, WebP ou GIF."
        )
    if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
        raise ImageError(
            f"Image trop grande ({img.width}x{img.height} px) — maximum {MAX_DIMENSION}x{MAX_DIMENSION} px."
        )
    if img.width < 1 or img.height < 1:
        raise ImageError("Image invalide (dimensions nulles).")

    # Force le décodage complet des pixels maintenant (plutôt que paresseusement
    # plus tard), pour que toute anomalie remonte ici comme une erreur propre.
    img.load()
    return img


def _compress_under_limit(img, max_bytes=MAX_IMAGE_BYTES):
    """Compresse en JPEG en réduisant progressivement la qualité, puis si besoin
    les dimensions, jusqu'à passer sous la limite de poids."""
    work_img = img
    for _round in range(6):
        quality = 85
        while quality >= MIN_JPEG_QUALITY:
            buf = BytesIO()
            to_save = work_img.convert("RGB") if work_img.mode != "RGB" else work_img
            to_save.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data, "image/jpeg"
            quality -= 10

        w, h = work_img.size
        if w <= 300 or h <= 300:
            break
        work_img = work_img.resize((int(w * 0.8), int(h * 0.8)), Image.LANCZOS)

    raise ImageError("Impossible de compresser l'image sous 200 Ko, même après réduction des dimensions.")


def process_upload(file_bytes, crop_box=None):
    """Valide, recadre si demandé (crop_box = (left, top, right, bottom) en
    pixels de l'image ORIGINALE), ré-encode entièrement et compresse sous
    200 Ko. Retourne (bytes, mimetype)."""
    img = _open_and_validate(file_bytes)

    if crop_box:
        left, top, right, bottom = crop_box
        left, top = max(0, int(left)), max(0, int(top))
        right, bottom = min(img.width, int(right)), min(img.height, int(bottom))
        if right <= left or bottom <= top:
            raise ImageError("Zone de recadrage invalide.")
        img = img.crop((left, top, right, bottom))

    return _compress_under_limit(img)
