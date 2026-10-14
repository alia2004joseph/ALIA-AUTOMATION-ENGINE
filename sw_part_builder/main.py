"""Run the Part Builder UI or inspect a PDF drawing locally."""
import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--inspect-pdf")
    args, remaining = parser.parse_known_args()
    if args.inspect_pdf:
        from partbuilder.core.pdf_inspector import main as inspect_pdf_main

        raise SystemExit(inspect_pdf_main([args.inspect_pdf, *remaining]))
    from partbuilder.ui.app import App

    App().mainloop()
