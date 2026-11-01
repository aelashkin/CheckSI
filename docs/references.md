# References

CheckSI implements or relies on the methods, models, data and software below. These references are separate from the [CheckSI citation](../README.md#citation). Cite the entries your results depend on; DP5q scores, for example, depend on the DP5q, DP5 and CASCADE papers.

## DP5q scoring and models

1. Kotlyarov, R.; Howarth, A.; Goodman, J. M. DP5 without DFT: uncertainty-calibrated graph neural net accelerates structure confirmation via NMR. *Chem. Sci.* **2026**, *17* (18), 9138–9147. [DOI 10.1039/D5SC06988B](https://doi.org/10.1039/D5SC06988B). DP5q method, reimplemented offline for the advisory ¹³C compatibility score.
2. Howarth, A.; Goodman, J. M. The DP5 probability, quantification and visualisation of structural uncertainty in single molecules. *Chem. Sci.* **2022**, *13* (12), 3507–3518. [DOI 10.1039/D1SC04406K](https://doi.org/10.1039/D1SC04406K). DP5 probability framework that DP5q extends.
3. Guan, Y.; Shree Sowndarya, S. V.; Gallegos, L. C.; St. John, P. C.; Paton, R. S. Real-time prediction of ¹H and ¹³C chemical shifts with DFT accuracy using a 3D graph neural network. *Chem. Sci.* **2021**, *12* (36), 12012–12026. [DOI 10.1039/D1SC03343C](https://doi.org/10.1039/D1SC03343C). CASCADE architecture of the bundled ¹³C predictors.
4. St. John, P. C.; Phillips, C.; Kemper, T. W.; Wilson, A. N.; Guan, Y.; Crowley, M. F.; Nimlos, M. R.; Larsen, R. E. Message-passing neural networks for high-throughput polymer screening. *J. Chem. Phys.* **2019**, *150* (23), 234111. [DOI 10.1063/1.5099132](https://doi.org/10.1063/1.5099132). Message-passing layer design reimplemented in NumPy.
5. St. John, P.; Ward, L.; Shree Sowndarya, S. V. nfp (Neural fingerprint) [software]. Zenodo. [DOI 10.5281/zenodo.5899628](https://doi.org/10.5281/zenodo.5899628). Graph layers used by the upstream models; CheckSI does not import NFP.
6. Goodman Lab. DP5 [software], [Goodman-lab/DP5](https://github.com/Goodman-lab/DP5). DP5q models from the [ruslankotl/DP5](https://github.com/ruslankotl/DP5) fork, frozen at [aelashkin/DP5 `8f8e223`](https://github.com/aelashkin/DP5/tree/8f8e22364654bbce71dcb6abb414eee793b00d92). Source of the parameters, exported without retraining.
7. Smith, S. G.; Goodman, J. M. Assigning stereochemistry to single diastereoisomers by GIAO NMR calculation: the DP4 probability. *J. Am. Chem. Soc.* **2010**, *132* (37), 12946–12959. [DOI 10.1021/ja105035r](https://doi.org/10.1021/ja105035r). Linear scaling for the exported corrected-MAE diagnostic only; not part of the score.

## Spectral and atomic data

8. Kuhn, S.; Schlörer, N. E. Facilitating quality control for spectra assignments of small organic molecules: nmrshiftdb2 – a free in-house NMR database with integrated LIMS for academic service laboratories. *Magn. Reson. Chem.* **2015**, *53* (8), 582–589. [DOI 10.1002/mrc.4263](https://doi.org/10.1002/mrc.4263). Experimental shifts behind the predictor training data; no records ship.
9. Steinbeck, C.; Krause, S.; Kuhn, S. NMRShiftDB – constructing a free chemical information system with open-source components. *J. Chem. Inf. Comput. Sci.* **2003**, *43* (6), 1733–1739. [DOI 10.1021/ci0341363](https://doi.org/10.1021/ci0341363). Original NMRShiftDB publication.
10. [nmrdb-dataset](https://github.com/aelashkin/DP5/tree/8f8e22364654bbce71dcb6abb414eee793b00d92/nmrdb-dataset): nmrshiftdb2-derived training dataset and preparation method for the DFT-free DP5 models.
11. Coursey, J. S.; Schwab, D. J.; Tsai, J. J.; Dragoset, R. A. *Atomic Weights and Isotopic Compositions* (version 4.1); National Institute of Standards and Technology: Gaithersburg, MD, 2015. [physics.nist.gov/Comp](http://physics.nist.gov/Comp). Element data source of molmass, behind most masses, abundances and atomic weights.
12. CIAAW. *Isotopic compositions of the elements 2024*. [ciaaw.org](https://www.ciaaw.org/isotopic-abundances.htm). ¹⁹⁸Hg abundance used to classify natural isotope peaks.
13. Hoch, J. C.; Baskaran, K.; Burr, H.; et al. Biological Magnetic Resonance Data Bank. *Nucleic Acids Res.* **2023**, *51* (D1), D368–D376. [DOI 10.1093/nar/gkac1050](https://doi.org/10.1093/nar/gkac1050). Four metabolomics entries used only as development fixtures.

## Structure and conformer methods

14. Lowe, D. M.; Corbett, P. T.; Murray-Rust, P.; Glen, R. C. Chemical name to structure: OPSIN, an open source solution. *J. Chem. Inf. Model.* **2011**, *51* (3), 739–753. [DOI 10.1021/ci100384d](https://doi.org/10.1021/ci100384d). Local name-to-structure conversion (OPSIN 2.7.0 through pyopsin 0.2.4).
15. Riniker, S.; Landrum, G. A. Better informed distance geometry: using what we know to improve conformation generation. *J. Chem. Inf. Model.* **2015**, *55* (12), 2562–2574. [DOI 10.1021/acs.jcim.5b00654](https://doi.org/10.1021/acs.jcim.5b00654). ETKDG conformer embedding.
16. Wang, S.; Witek, J.; Landrum, G. A.; Riniker, S. Improving conformer generation for small rings and macrocycles based on distance geometry and experimental torsional-angle preferences. *J. Chem. Inf. Model.* **2020**, *60* (4), 2044–2058. [DOI 10.1021/acs.jcim.0c00025](https://doi.org/10.1021/acs.jcim.0c00025). ETKDGv3, used for DP5q and structure geometries.
17. Halgren, T. A. Merck molecular force field. I. Basis, form, scope, parameterization, and performance of MMFF94. *J. Comput. Chem.* **1996**, *17* (5–6), 490–519. [DOI 10.1002/(SICI)1096-987X(199604)17:5/6<490::AID-JCC1>3.0.CO;2-P](https://doi.org/10.1002/%28SICI%291096-987X%28199604%2917%3A5%2F6%3C490%3A%3AAID-JCC1%3E3.0.CO%3B2-P). MMFF94 typing and parameter availability.
18. Halgren, T. A. MMFF VI. MMFF94s option for energy minimization studies. *J. Comput. Chem.* **1999**, *20* (7), 720–729. [DOI 10.1002/(SICI)1096-987X(199905)20:7<720::AID-JCC7>3.0.CO;2-X](https://doi.org/10.1002/%28SICI%291096-987X%28199905%2920%3A7%3C720%3A%3AAID-JCC7%3E3.0.CO%3B2-X). MMFF94s conformer minimisation for DP5q.
19. Tosco, P.; Stiefl, N.; Landrum, G. Bringing the MMFF force field to the RDKit: implementation and validation. *J. Cheminform.* **2014**, *6* (1), 37. [DOI 10.1186/s13321-014-0037-3](https://doi.org/10.1186/s13321-014-0037-3). RDKit MMFF implementation that CheckSI calls.
20. Schneider, N.; Sayle, R. A.; Landrum, G. A. Get your atoms in order—an open-source implementation of a novel and robust molecular canonicalization algorithm. *J. Chem. Inf. Model.* **2015**, *55* (10), 2111–2120. [DOI 10.1021/acs.jcim.5b00543](https://doi.org/10.1021/acs.jcim.5b00543). Canonical ranking for atom symmetry classes in NMR counts.
21. Hill, E. A. On a system of indexing chemical literature; adopted by the Classification Division of the U. S. Patent Office. *J. Am. Chem. Soc.* **1900**, *22* (8), 478–494. [DOI 10.1021/ja02046a005](https://doi.org/10.1021/ja02046a005). Hill formula order.

## Numerical methods

22. Harris, C. R.; Millman, K. J.; van der Walt, S. J.; et al. Array programming with NumPy. *Nature* **2020**, *585* (7825), 357–362. [DOI 10.1038/s41586-020-2649-2](https://doi.org/10.1038/s41586-020-2649-2). Array implementation of the DP5q network (NumPy 2.4.2).
23. Virtanen, P.; Gommers, R.; Oliphant, T. E.; et al. SciPy 1.0: fundamental algorithms for scientific computing in Python. *Nat. Methods* **2020**, *17* (3), 261–272. [DOI 10.1038/s41592-019-0686-2](https://doi.org/10.1038/s41592-019-0686-2). Quantile fits, normal tails and assignment (SciPy 1.18.1).
24. Crouse, D. F. On implementing 2D rectangular assignment algorithms. *IEEE Trans. Aerosp. Electron. Syst.* **2016**, *52* (4), 1679–1696. [DOI 10.1109/TAES.2016.140952](https://doi.org/10.1109/TAES.2016.140952). Algorithm behind the predicted-to-observed shift assignment.
25. Branch, M. A.; Coleman, T. F.; Li, Y. A subspace, interior, and conjugate gradient method for large-scale bound-constrained minimization problems. *SIAM J. Sci. Comput.* **1999**, *21* (1), 1–23. [DOI 10.1137/S1064827595289108](https://doi.org/10.1137/S1064827595289108). Trust-region-reflective fit of each predicted quantile distribution.

## Software

26. RDKit: Open-source cheminformatics, version 2025.09.3. [rdkit.org](https://www.rdkit.org); [DOI 10.5281/zenodo.17746401](https://doi.org/10.5281/zenodo.17746401).
27. Huang, D. pyopsin, version 0.2.4 [software]. [Dingyun-Huang/pyopsin](https://github.com/Dingyun-Huang/pyopsin).
28. Gohlke, C. molmass, version 2026.1.8 [software]. Zenodo, 2026. [DOI 10.5281/zenodo.18187897](https://doi.org/10.5281/zenodo.18187897).
29. Kienzle, P.; et al. periodictable, version 2.0.2 [software]. Zenodo, 2024. [DOI 10.5281/zenodo.14533095](https://doi.org/10.5281/zenodo.14533095).
30. Artifex Software. PyMuPDF, version 1.26.7 [software]. [pymupdf/PyMuPDF](https://github.com/pymupdf/PyMuPDF).
31. Canny, S. python-docx, version 1.2.0 [software]. [python-openxml/python-docx](https://github.com/python-openxml/python-docx).
32. The Document Foundation. LibreOffice [software]. [libreoffice.org](https://www.libreoffice.org). Optional DOCX-to-PDF conversion for source annotation.

Versions are those locked in `uv.lock`. Bundled-asset terms are on the [licenses and notices](licensing.md) page.
