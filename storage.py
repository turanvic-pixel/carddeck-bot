import json
import random
import logging

from github import Github, Auth

logger = logging.getLogger(__name__)


class CardStorage:
    """Хранит карточки (file_id фото + текст) в cards.json в GitHub-репозитории.

    SQLite на бесплатном Render не подходит: диск стирается при каждом
    рестарте/редеплое сервиса. GitHub-репозиторий переживает это без проблем.
    """

    def __init__(self, github_token: str, repo_name: str, file_path: str = "cards.json"):
        self.repo = Github(auth=Auth.Token(github_token)).get_user().get_repo(repo_name)
        self.file_path = file_path
        self.cards = []
        self._sha = None
        self._load()

    def _load(self):
        try:
            f = self.repo.get_contents(self.file_path)
            data = json.loads(f.decoded_content.decode())
            self.cards = data.get("cards", [])
            self._sha = f.sha
            logger.info("Загружено карточек: %d", len(self.cards))
        except Exception as e:
            logger.warning("cards.json не найден, стартуем с пустого списка: %s", e)
            self.cards = []
            self._sha = None

    def _save(self, commit_message: str):
        content = json.dumps({"cards": self.cards}, ensure_ascii=False, indent=2)
        if self._sha:
            result = self.repo.update_file(self.file_path, commit_message, content, self._sha)
        else:
            result = self.repo.create_file(self.file_path, commit_message, content)
        self._sha = result["content"].sha

    def add_card(self, file_id: str, kind: str = "photo") -> int:
        new_id = max((c["id"] for c in self.cards), default=0) + 1
        self.cards.append({"id": new_id, "file_id": file_id, "kind": kind})
        self._save(f"add card #{new_id}")
        return new_id

    def random_card(self):
        if not self.cards:
            return None
        return random.choice(self.cards)

    def count(self) -> int:
        return len(self.cards)
