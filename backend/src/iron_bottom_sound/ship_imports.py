from __future__ import annotations

import base64
import binascii
from io import BytesIO
import re
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, model_validator

from .models import ShipRecord


class ShipCatalogImport(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    english_name: str = Field(min_length=1, max_length=120)
    class_name: str = Field(min_length=1, max_length=120)
    ship_type: str = Field(min_length=1, max_length=12)
    source_table: int = Field(ge=0)
    source_row: int = Field(ge=0)


class ShipImportRequest(BaseModel):
    catalog: ShipCatalogImport
    record: ShipRecord

    @model_validator(mode="after")
    def matching_identity(self):
        if not re.fullmatch(r"IBS-U-[A-Z0-9][A-Z0-9-]{1,80}", self.record.id):
            raise ValueError("船只 ID 必须使用 IBS-U- 前缀，只含大写字母、数字和连字符")
        if self.record.name != self.catalog.name:
            raise ValueError("catalog.name 必须与 record.name 一致")
        if self.record.ship_type != self.catalog.ship_type:
            raise ValueError("catalog.ship_type 必须与 record.ship_type 一致")
        if not any(mount.kind == "primary" for mount in self.record.guns):
            raise ValueError("舰船至少需要一个主炮炮位，才能用于规则引擎")
        return self

    def catalog_payload(self) -> dict[str, Any]:
        return self.catalog.model_dump(mode="json")

    def record_payload(self) -> dict[str, Any]:
        return self.record.model_dump(mode="json")


class ShipImportEnvelope(BaseModel):
    definition: ShipImportRequest
    asset_image_base64: str = Field(min_length=100, max_length=11_184_812)

    def image_payload(self) -> tuple[str, bytes]:
        try:
            image_bytes = base64.b64decode(self.asset_image_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("算子图片不是有效的 Base64 文件") from error
        if len(image_bytes) > 8 * 1024 * 1024:
            raise ValueError("算子图片不能超过 8 MiB")
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("算子图片仅支持 PNG、JPEG 或 WebP")
                if width < 32 or height < 16 or width * height > 20_000_000:
                    raise ValueError("算子图片尺寸需至少 32×16，且不超过 2000 万像素")
                image.verify()
        except (UnidentifiedImageError, OSError) as error:
            raise ValueError("无法解码算子图片，请选择有效的 PNG、JPEG 或 WebP 文件") from error
        with Image.open(BytesIO(image_bytes)) as image:
            artwork = ImageOps.exif_transpose(image).convert("RGBA")
            artwork.thumbnail((224, 144), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (224, 144), (0, 0, 0, 0))
            canvas.alpha_composite(
                artwork,
                ((224 - artwork.width) // 2, (144 - artwork.height) // 2),
            )
            normalized = BytesIO()
            canvas.save(normalized, format="PNG", optimize=True)
        return "image/png", normalized.getvalue()
