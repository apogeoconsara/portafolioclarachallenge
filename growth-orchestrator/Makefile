N ?= 50000
SEED ?= 42

.PHONY: data seed profile all test
data:      ## build the full synthetic world (tables + event stream + sqlite)
	python3 -m generator build --seed $(SEED) --n $(N)
seed:      ## regenerate frozen seed artefacts (reply seeds, golden set, eval cases, sample)
	python3 -m generator seed
profile:   ## data profile + validation checks
	python3 -m generator profile
all:
	python3 -m generator all --seed $(SEED) --n $(N)
test:
	python3 -m unittest discover -s tests -t .
