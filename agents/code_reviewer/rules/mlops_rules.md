# Mandatory MLOps review rules

## Preprocessing
Fit scalers, encoders, imputers and feature selectors on the training split only,
then apply them to validation and test data. Prefer an sklearn `Pipeline`.

## Splitting
Split before any data-dependent preprocessing. Use `stratify=y` for imbalanced
classification. Never shuffle time-ordered data across the split boundary.

## Reproducibility
Every random operation (splits, model init, sampling, shuffling) needs a fixed seed
passed explicitly, for example `random_state=42`.

## Evaluation
Report final metrics on held-out data only. A metric computed on training data must
be labelled as training performance.

## Out of scope for automatic changes
Model architecture, features, preprocessing behaviour, data splits, hyperparameters,
evaluation thresholds and dependency versions are only ever reported, never changed
automatically.
