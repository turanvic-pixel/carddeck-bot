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
        self._decks = {}
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

    def next_card_for_user(self, user_id: int):
        """Тянет карточку без повторов, пока не покажет все — потом тасует заново."""
        if not self.cards:
            return None
        deck = self._decks.get(user_id)
        if not deck:
            deck = [c["id"] for c in self.cards]
            random.shuffle(deck)
            self._decks[user_id] = deck
        card_id = deck.pop()
        card = next((c for c in self.cards if c["id"] == card_id), None)
        if card is None:
            # карточку успели удалить между тасовками — тянем следующую
            return self.next_card_for_user(user_id)
        return card

    def delete_card(self, card_id: int) -> bool:
        before = len(self.cards)
        self.cards = [c for c in self.cards if c["id"] != card_id]
        if len(self.cards) == before:
            return False
        self._save(f"delete card #{card_id}")
        return True

    def list_ids(self) -> list:
        return [c["id"] for c in self.cards]

    def count(self) -> int:
        return len(self.cards)


class FavoritesStore:
    """Избранные карточки пользователей — favorites.json в GitHub."""

    def __init__(self, github_token: str, repo_name: str, file_path: str = "favorites.json"):
        self.repo = Github(auth=Auth.Token(github_token)).get_user().get_repo(repo_name)
        self.file_path = file_path
        self.data = {}  # str(user_id) -> [card_id, ...]
        self._sha = None
        self._load()

    def _load(self):
        try:
            f = self.repo.get_contents(self.file_path)
            self.data = json.loads(f.decoded_content.decode())
            self._sha = f.sha
        except Exception as e:
            logger.warning("favorites.json не найден, стартуем с пустого: %s", e)
            self.data = {}
            self._sha = None

    def _save(self, commit_message: str):
        content = json.dumps(self.data, ensure_ascii=False, indent=2)
        if self._sha:
            result = self.repo.update_file(self.file_path, commit_message, content, self._sha)
        else:
            result = self.repo.create_file(self.file_path, commit_message, content)
        self._sha = result["content"].sha

    def add(self, user_id: int, card_id: int) -> bool:
        key = str(user_id)
        favs = self.data.setdefault(key, [])
        if card_id in favs:
            return False
        favs.append(card_id)
        self._save(f"favorite add user={user_id} card={card_id}")
        return True

    def list_for_user(self, user_id: int) -> list:
        return self.data.get(str(user_id), [])


class ReminderStore:
    """Кто и в какое время (UTC) хочет получать карточку дня — reminders.json в GitHub."""

    def __init__(self, github_token: str, repo_name: str, file_path: str = "reminders.json"):
        self.repo = Github(auth=Auth.Token(github_token)).get_user().get_repo(repo_name)
        self.file_path = file_path
        self.data = {}  # str(user_id) -> "HH:MM"
        self._sha = None
        self._load()

    def _load(self):
        try:
            f = self.repo.get_contents(self.file_path)
            self.data = json.loads(f.decoded_content.decode())
            self._sha = f.sha
        except Exception as e:
            logger.warning("reminders.json не найден, стартуем с пустого: %s", e)
            self.data = {}
            self._sha = None

    def _save(self, commit_message: str):
        content = json.dumps(self.data, ensure_ascii=False, indent=2)
        if self._sha:
            result = self.repo.update_file(self.file_path, commit_message, content, self._sha)
        else:
            result = self.repo.create_file(self.file_path, commit_message, content)
        self._sha = result["content"].sha

    def set(self, user_id: int, time_str: str):
        self.data[str(user_id)] = time_str
        self._save(f"reminder set user={user_id} time={time_str}")

    def remove(self, user_id: int) -> bool:
        key = str(user_id)
        if key not in self.data:
            return False
        del self.data[key]
        self._save(f"reminder remove user={user_id}")
        return True

    def all(self) -> dict:
        return dict(self.data)
