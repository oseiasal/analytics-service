import json
from unittest.mock import MagicMock
import app as flask_app_module


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_process_message_success(mock_aws):
    mock_sqs = mock_aws["sqs"]
    mock_dynamo = mock_aws["dynamodb"]

    message_payload = {
        "user_id": "user-123",
        "flag_name": "feature-beta",
        "result": True,
        "timestamp": "2026-08-15T12:00:00Z",
    }

    message = {
        "MessageId": "msg-001",
        "Body": json.dumps(message_payload),
        "ReceiptHandle": "receipt-handle-001",
    }

    flask_app_module.process_message(message)

    # Verifica que inseriu no DynamoDB
    mock_dynamo.put_item.assert_called_once()
    call_args = mock_dynamo.put_item.call_args[1]
    assert call_args["TableName"] == "analytics_events"
    assert call_args["Item"]["user_id"]["S"] == "user-123"
    assert call_args["Item"]["flag_name"]["S"] == "feature-beta"
    assert call_args["Item"]["result"]["BOOL"] is True

    # Verifica que deletou a mensagem do SQS
    mock_sqs.delete_message.assert_called_once_with(
        QueueUrl="http://localhost:4566/000000000000/analytics-queue",
        ReceiptHandle="receipt-handle-001",
    )


def test_process_message_invalid_json(mock_aws):
    mock_sqs = mock_aws["sqs"]
    mock_dynamo = mock_aws["dynamodb"]

    message = {
        "MessageId": "msg-poison",
        "Body": "invalid-non-json-string",
        "ReceiptHandle": "receipt-handle-poison",
    }

    flask_app_module.process_message(message)

    # Não deve inserir nem deletar mensagem inválida
    mock_dynamo.put_item.assert_not_called()
    mock_sqs.delete_message.assert_not_called()
