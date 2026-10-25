from __future__ import annotations

import argparse

from triage.training.dataset import normalize_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="Normaliza o CSV do corpus.")
    parser.add_argument("--data", default="data/raw/medical_tc_train.csv")
    parser.add_argument("--output", default="data/interim/medical_tc_train_normalized.csv")
    args = parser.parse_args()
    normalize_csv(args.data, args.output)


if __name__ == "__main__":
    main()
