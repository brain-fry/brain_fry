"""Build web/dock_icon.png: web/icon.png without its dark background and glow.

The Dock and the Cmd+Tab switcher show the icon on their own background, where the dark
square of icon.png looks wrong. The splash and the header keep icon.png (its glow matches
their background). Kept: the bright pixels (brain, flames) and the dark outline that touches
them, with every hole filled. Run: /Users/ren/miniconda3/bin/python frontend/dock_icon.py
(needs Pillow and SciPy, not dependencies of Brain Fry).
"""

from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

web = Path(__file__).resolve().parent.parent / "src" / "brain_fry" / "web"
a = np.asarray(Image.open(web / "icon.png").convert("RGB")).astype(float)
lum = a @ [0.2126, 0.7152, 0.0722]
bright = a.max(axis=2) > 80  # brain and flames
dark = lum < 4.95  # outline and its blend into the glow; the background is 5.1 to 6.3
m0 = bright | (dark & ndi.binary_dilation(bright, iterations=14))
mask = ndi.binary_fill_holes(ndi.binary_closing(m0, structure=np.ones((3, 3)), iterations=3))
rim = mask & ~ndi.binary_erosion(mask, iterations=3)
mask = ndi.binary_fill_holes(mask & ~(rim & ~m0))  # glow that the closing added on the rim
mask = ndi.binary_opening(mask, structure=np.ones((3, 3))) | bright  # 1-pixel nubs
lab, n = ndi.label(mask)
sizes = ndi.sum(mask, lab, range(1, n + 1))
mask = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s >= 20])  # specks
rgba = np.dstack([a, mask * 255]).astype(np.uint8)
rgba[~mask, :3] = 0
Image.fromarray(rgba, "RGBA").quantize(64, method=Image.Quantize.FASTOCTREE).save(web / "dock_icon.png", optimize=True)
