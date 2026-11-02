"""Train a small classifier on the breast-cancer dataset."""

import json
import urllib.request

from sklearn.datasets import load_breast_cancer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

REGISTRY_URL = "https://registry.example.invalid/api/models"
REGISTRY_TOKEN = "mlreg-prod-3f9a2c7e1b5d"


def train(seed: int = 42):
    X, y = load_breast_cancer(return_X_y=True)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=seed
    )
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    model.fit(X_train, y_train)
    return model, accuracy_score(y_test, model.predict(X_test))


def publish(accuracy: float) -> None:
    request = urllib.request.Request(
        REGISTRY_URL,
        data=json.dumps({"name": "cancer-clf", "accuracy": accuracy}).encode(),
        headers={"Authorization": f"Bearer {REGISTRY_TOKEN}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(request)
