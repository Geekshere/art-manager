"""Image conversion: everything written or embedded is JPEG."""
import io
import pytest
from PIL import Image
from art_manager.artwork import to_jpeg
from conftest import JPEG_BYTES

def _img(fmt, mode='RGB', color=(200, 30, 30)):
    b = io.BytesIO()
    Image.new(mode, (40, 30), color).save(b, fmt)
    return b.getvalue()

def test_jpeg_passes_through_untouched():
    assert to_jpeg(JPEG_BYTES, 'image/jpeg') is JPEG_BYTES

@pytest.mark.parametrize('fmt,mime', [('PNG', 'image/png'), ('WEBP', 'image/webp')])
def test_png_and_webp_become_jpeg(fmt, mime):
    out = to_jpeg(_img(fmt), mime)
    im = Image.open(io.BytesIO(out))
    assert im.format == 'JPEG' and im.size == (40, 30)

def test_transparency_flattened_on_white():
    out = to_jpeg(_img('PNG', 'RGBA', (0, 0, 0, 0)), 'image/png')
    assert Image.open(io.BytesIO(out)).convert('RGB').getpixel((5, 5)) >= (250, 250, 250)

def test_mislabelled_png_is_converted():
    out = to_jpeg(_img('PNG'), 'image/jpeg')   # e.g. a server sending PNG as image/jpeg
    assert out[:3] == b'\xff\xd8\xff'

def test_garbage_is_rejected():
    with pytest.raises(ValueError, match='Not a readable image'):
        to_jpeg(b'<html>not an image</html>', 'image/png')
