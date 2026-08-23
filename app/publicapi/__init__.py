"""The external (non-human) HTTP surface. One route: POST /v1/ocr.

Nothing here may import `rapidocr`, `onnxruntime`, `cv2` or any part of
`docling` at module scope — `app/files/image_ocr.py` owns those imports and
keeps them inside functions, so the API image can run with the OCR stack absent
(it then answers 503). A subprocess test enforces this.
"""
