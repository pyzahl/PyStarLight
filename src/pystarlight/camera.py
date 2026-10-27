"""Launcher of the capture app (capture/app.py) for the Pi's system Python.

    python3 src/pystarlight/camera.py [options]          (see --help)

Run as a file, `capture` is imported as a top-level package from this folder, so the rest of
PyStarLight (scikit-image, astropy ...) is not needed. With the full package installed,
`python -m pystarlight.capture` does the same.
"""
if __name__ == "__main__":
    from capture.app import main
    main()
