TEX  = pdflatex
BIB  = bibtex
MAIN = main
ARXIV_ZIP = resilient-write-arxiv.zip

.PHONY: all clean arxiv

all: $(MAIN).pdf

$(MAIN).pdf: $(MAIN).tex references.bib
	$(TEX) $(MAIN)
	$(BIB) $(MAIN)
	$(TEX) $(MAIN)
	$(TEX) $(MAIN)

arxiv: $(MAIN).pdf
	rm -f $(ARXIV_ZIP)
	rm -f *.log *.out *.toc *.fls *.fdb_latexmk *.synctex.gz
	zip $(ARXIV_ZIP) \
		$(MAIN).tex \
		$(MAIN).bbl \
		references.bib \
		fig_architecture.tex \
		fig_failure_comparison.pdf \
		fig_layer_coverage.pdf \
		fig_test_coverage.pdf

clean:
	rm -f *.aux *.bbl *.blg *.log *.out *.toc *.fls *.fdb_latexmk *.synctex.gz main.pdf $(ARXIV_ZIP)
