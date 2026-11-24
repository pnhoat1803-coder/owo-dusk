Vendored pure-Python packages, so the project does not need them installed on every PC.

pytesseract 0.3.13 (Apache License 2.0, see pytesseract\LICENSE)
  Source: https://pypi.org/project/pytesseract/0.3.13/  (files copied unchanged)
  Needs: Pillow and packaging (both are installed by setup.py from requirements.txt).
  core\level_ocr.py imports an installed pytesseract first and only falls back to this copy.
