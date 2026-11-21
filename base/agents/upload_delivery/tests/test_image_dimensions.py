from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from base.agents.upload_delivery.models import Manifest, Object
from base.agents.upload_delivery.paths import staged_path
from base.agents.upload_delivery.source import partition_delivered_uploads


def _image_bytes(size: tuple[int, int], fmt: str) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=(12, 34, 56)).save(buf, format=fmt)
    return buf.getvalue()


def test_partition_delivered_uploads_records_dimensions_for_staged_images_and_ignores_invalid(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "staged-root"
    png_bytes = _image_bytes((640, 360), "PNG")
    jpg_bytes = _image_bytes((300, 500), "JPEG")
    bmp_bytes = _image_bytes((120, 80), "BMP")
    bad_png = b"not-a-real-png"
    pdf_bytes = b"%PDF-1.4\n"

    files = [
        ("1" * 32, "shot.png", "image/png", png_bytes),
        ("2" * 32, "photo.jpg", "image/jpeg", jpg_bytes),
        ("3" * 32, "broken.png", "image/png", bad_png),
        ("4" * 32, "bitmap.bmp", "image/bmp", bmp_bytes),
        ("5" * 32, "doc.pdf", "application/pdf", pdf_bytes),
    ]
    objects: list[Object] = []
    delivered_paths: list[str] = []
    for ordinal, (staged_id, name, content_type, data) in enumerate(files):
        sp = staged_path(storage_dir, staged_id)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_bytes(data)
        objects.append(
            Object(
                ordinal=ordinal,
                name=name,
                staged_id=staged_id,
                size=len(data),
                sha256="a" * 64,
                content_type=content_type,
            )
        )
        delivered_paths.append(f"/remote/workspaces/7/uploads/20261121-210000/{name}")

    manifest = Manifest(
        batch_id="a" * 32,
        agent_id=7,
        sent_at="2026-11-21T21:00:00+00:00",
        directory="20261121-210000",
        objects=objects,
    )

    image_uploads, file_uploads = partition_delivered_uploads(
        manifest, delivered_paths, storage_directory=str(storage_dir)
    )

    assert len(image_uploads) == 3
    assert (image_uploads[0]["width"], image_uploads[0]["height"]) == (640, 360)
    assert (image_uploads[1]["width"], image_uploads[1]["height"]) == (300, 500)
    assert "width" not in image_uploads[2] and "height" not in image_uploads[2]

    assert len(file_uploads) == 2
    assert (file_uploads[0]["width"], file_uploads[0]["height"]) == (120, 80)
    assert "width" not in file_uploads[1] and "height" not in file_uploads[1]


def test_partition_delivered_uploads_falls_back_to_delivered_path_when_staged_missing(
    tmp_path: Path,
) -> None:
    png_bytes = _image_bytes((400, 200), "PNG")
    delivered = tmp_path / "shot.png"
    delivered.write_bytes(png_bytes)

    manifest = Manifest(
        batch_id="b" * 32,
        agent_id=7,
        sent_at="2026-11-21T21:00:00+00:00",
        directory="20261121-210000",
        objects=[
            Object(
                ordinal=0,
                name="shot.png",
                staged_id="f" * 32,
                size=len(png_bytes),
                sha256="b" * 64,
                content_type="image/png",
            )
        ],
    )

    image_uploads, file_uploads = partition_delivered_uploads(
        manifest, [str(delivered)], storage_directory=str(tmp_path / "empty")
    )

    assert file_uploads == []
    assert len(image_uploads) == 1
    assert (image_uploads[0]["width"], image_uploads[0]["height"]) == (400, 200)


def test_partition_delivered_uploads_swaps_exif_rotated_portrait_jpeg(tmp_path: Path) -> None:
    storage_dir = tmp_path / "staged-exif"
    buf = BytesIO()
    img = Image.new("RGB", (800, 400), color=(40, 80, 120))
    exif = img.getexif()
    exif[274] = 6
    img.save(buf, format="JPEG", exif=exif)
    jpg_bytes = buf.getvalue()

    staged_id = "9" * 32
    sp = staged_path(storage_dir, staged_id)
    sp.parent.mkdir(parents=True, exist_ok=True)
    _ = sp.write_bytes(jpg_bytes)

    manifest = Manifest(
        batch_id="c" * 32,
        agent_id=7,
        sent_at="2026-11-21T22:00:00+00:00",
        directory="20261121-220000",
        objects=[
            Object(
                ordinal=0,
                name="portrait.jpg",
                staged_id=staged_id,
                size=len(jpg_bytes),
                sha256="c" * 64,
                content_type="image/jpeg",
            )
        ],
    )

    image_uploads, _ = partition_delivered_uploads(
        manifest,
        ["/remote/workspaces/7/uploads/20261121-220000/portrait.jpg"],
        storage_directory=str(storage_dir),
    )

    assert len(image_uploads) == 1
    assert (image_uploads[0]["width"], image_uploads[0]["height"]) == (400, 800)
