"""fresh DB の組み込み標準フォーマット seed。"""

from conftest import auth_headers, issue_access_token, make_user
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def test_builtin_format_is_seeded_on_first_list(client: TestClient, db: Session) -> None:
    user = make_user(db)
    db.commit()
    token = issue_access_token(db, user)

    response = client.get("/v1/formats", headers=auth_headers(token))

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["profile_id"] == "00000000-0000-4000-8000-000000000001"
