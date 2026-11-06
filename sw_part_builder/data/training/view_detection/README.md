# Drawing View Starter Data

This bootstrap set is for the first model: detecting and classifying drawing
views before dimension extraction.

It contains four source drawings and 17 view regions, labeled as orthographic,
section, detail, or isometric. The PNGs are clean page images rendered from the
fixture PDFs; colored analyzer boxes are not part of the model inputs. The
labels were checked against the drawing pages. Bounding boxes originate from
the raster analyzer's source-coordinate proposals and can include callouts, so
review and tighten them before using this set for a production detector.

`annotations.json` stores page-level samples. Keep all views from one drawing
together when creating train, validation, and test splits. This starter set
places all four drawings in `train`; it is too small to measure generalization.
It has only one detail-view example, so more independently sourced drawings
with section and detail views are needed before training a useful classifier.

The source PDFs are existing local fixtures. Check their usage terms before
redistributing the images or using them outside this local prototype.