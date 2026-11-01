"""Molecule images shared by the portable findings renderer."""
from io import BytesIO

from rdkit import Chem
from rdkit.Chem import Draw


def generate_molecule_image(smiles: str) -> BytesIO:
    """Return a PNG molecule drawing for the findings report."""
    image_size = (500, 500)
    line_width = 3.0
    font_size = 14
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError("Invalid SMILES: Unable to create molecule")

    drawer = Draw.rdMolDraw2D.MolDraw2DCairo(*image_size)
    drawer.SetLineWidth(line_width)  
    drawer.SetFontSize(font_size)  

    # Drawing the molecule
    drawer.DrawMolecule(molecule)
    drawer.FinishDrawing()

    # The image is obtained in PNG format
    img_data = drawer.GetDrawingText()
    image_stream = BytesIO(img_data)
    image_stream.seek(0)

    return image_stream
