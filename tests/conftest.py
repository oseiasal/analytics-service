import os
import sys
from unittest.mock import MagicMock, patch

# Ensure environment variables are set before app module loads
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_SQS_URL"] = "http://localhost:4566/000000000000/analytics-queue"
os.environ["AWS_DYNAMODB_TABLE"] = "analytics_events"
os.environ["AWS_ENDPOINT_URL"] = "http://localhost:4566"

# Create mock for boto3 and botocore if not installed
mock_boto3 = MagicMock()
mock_session = MagicMock()
mock_sqs = MagicMock()
mock_dynamodb = MagicMock()

mock_session.client.side_effect = lambda service, **kwargs: (
    mock_sqs if service == "sqs" else mock_dynamodb
)
mock_boto3.Session.return_value = mock_session

if "boto3" not in sys.modules:
    sys.modules["boto3"] = mock_boto3

if "botocore" not in sys.modules:
    mock_botocore = MagicMock()
    mock_botocore.exceptions.NoCredentialsError = Exception
    mock_botocore.exceptions.ClientError = Exception
    sys.modules["botocore"] = mock_botocore
    sys.modules["botocore.exceptions"] = mock_botocore.exceptions

import pytest

# Prevent background worker loop from spinning endlessly in pytest
with patch("threading.Thread"):
    import app as flask_app_module


@pytest.fixture
def app():
    """Provides the Flask application instance."""
    flask_app_module.app.config.update({"TESTING": True})
    return flask_app_module.app


@pytest.fixture
def client(app):
    """Provides a Flask test client."""
    return app.test_client()


@pytest.fixture
def mock_aws():
    """Provides mock AWS clients for SQS and DynamoDB."""
    sqs_mock = MagicMock()
    dynamodb_mock = MagicMock()
    flask_app_module.sqs_client = sqs_mock
    flask_app_module.dynamodb_client = dynamodb_mock
    return {
        "sqs": sqs_mock,
        "dynamodb": dynamodb_mock,
    }
