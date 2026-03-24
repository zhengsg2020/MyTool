.PHONY: run clean clean-all

run:
	python3 serve.py run

clean:
	python3 serve.py clean

clean-all:
	python3 serve.py clean --all
