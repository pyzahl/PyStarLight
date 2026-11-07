# PyStarLight method slides: make -> pystarlight_math.pdf
MAIN = pystarlight_math

all: $(MAIN).pdf

# the release the slides apply to (title slide), from the one place for it
version.tex: ../../src/pystarlight/__init__.py
	sed -nE 's/^__version__ = "([^"]*)".*/\\newcommand{\\pyversion}{\1}/p' $< > $@

$(MAIN).pdf: $(MAIN).tex preamble.tex math_slides.tex version.tex $(wildcard figures/*)
	latexmk -pdf -interaction=nonstopmode -halt-on-error $(MAIN).tex

clean:
	latexmk -c $(MAIN).tex
	rm -f version.tex

.PHONY: all clean
