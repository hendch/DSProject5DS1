from train import train


def test_accuracy_is_reasonable():
    _, accuracy = train()
    assert accuracy > 0.9
