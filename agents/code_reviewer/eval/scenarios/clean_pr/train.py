"""Train a small classifier on the breast-cancer dataset."""

from sklearn.datasets import load_breast_cancer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def train(seed: int = 42, max_iter: int = 2000):
    """Fit on a stratified split and return the model with its held-out accuracy."""
    X, y = load_breast_cancer(return_X_y=True)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=seed
    )
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=max_iter))
    model.fit(X_train, y_train)
    return model, accuracy_score(y_test, model.predict(X_test))
