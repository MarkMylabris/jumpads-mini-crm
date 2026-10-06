"""Демо-данные для БД.

    python -m scripts.seed            # 40 лидов и теги
    python -m scripts.seed --reset    # сначала удалить лиды и теги
    docker compose exec app python -m scripts.seed
"""

import argparse
import asyncio
import random
from datetime import timedelta

from sqlalchemy import delete

from app.db import SessionLocal
from app.models import Lead, LeadSource, LeadStatus, Message, MessageDirection, MessageKind, Tag, lead_tags, utcnow

TAGS = [("vip", "#d32f2f"), ("горячий", "#ff9800"), ("таргет", "#1e88e5"), ("SEO", "#43a047"), ("повторный", "#8e24aa")]
NAMES = ["Анна Смирнова", "Иван Петров", "Ольга Кузнецова", "Дмитрий Соколов", "Мария Попова", "Алексей Волков",
         "Екатерина Новикова", "Сергей Морозов", "Наталья Лебедева", "Павел Козлов", "Юлия Орлова", "Артём Зайцев"]
REQUESTS = ["Нужна настройка таргетированной рекламы ВКонтакте", "Хотим запустить контекст в Яндекс Директ",
            "Сколько стоит SEO-продвижение интернет-магазина?", "Нужен лендинг под новый продукт",
            "Интересует ведение Telegram-канала", "Аудит текущих рекламных кампаний",
            "Посевы в Telegram-каналах, бюджет 200 тыс.", "Разработка стратегии продвижения на квартал"]


async def seed(reset: bool, count: int) -> None:
    rnd = random.Random(42)
    async with SessionLocal() as session:
        if reset:
            for table in (lead_tags, Message.__table__, Lead.__table__, Tag.__table__):
                await session.execute(delete(table))
            await session.commit()

        existing = {t.name for t in (await session.execute(Tag.__table__.select())).all()}
        tags = [Tag(name=n, color=c) for n, c in TAGS if n not in existing]
        session.add_all(tags)
        await session.flush()
        all_tags = list((await session.execute(Tag.__table__.select())).all())
        tag_objs = [await session.get(Tag, row.id) for row in all_tags]

        now = utcnow()
        for i in range(count):
            source = rnd.choice(list(LeadSource))
            name = rnd.choice(NAMES)
            request = rnd.choice(REQUESTS)
            created = now - timedelta(days=rnd.randint(0, 30), hours=rnd.randint(0, 23), minutes=rnd.randint(0, 59))
            username = f"user{rnd.randint(1000, 9999)}"
            contact = {
                LeadSource.bot: f"+7999{rnd.randint(1000000, 9999999)}",
                LeadSource.telegram_account: f"@{username}",
                LeadSource.manual: f"{username}@example.com",
            }[source]
            status = rnd.choices(list(LeadStatus), weights=[5, 3, 2, 1])[0]
            lead = Lead(
                name=name,
                contact=contact,
                request=request,
                source=source,
                status=status,
                # отрицательные Telegram ID — чтобы не пересечься с реальными людьми
                telegram_user_id=None if source == LeadSource.manual else -(100000 + i),
                created_at=created,
                updated_at=created,
                tags=rnd.sample(tag_objs, k=rnd.randint(0, 2)),
            )
            msgs = []
            if source == LeadSource.bot:
                msgs.append(Message(direction=MessageDirection.inbound, created_at=created,
                                    text=f"Имя: {name}\nКонтакт: {contact}\nЗапрос: {request}"))
                msgs.append(Message(direction=MessageDirection.outbound, created_at=created,
                                    text="Заявка принята! Мы свяжемся с вами в ближайшее время."))
            elif source == LeadSource.telegram_account:
                msgs.append(Message(direction=MessageDirection.inbound, created_at=created, text=request))
                msgs.append(Message(direction=MessageDirection.outbound, created_at=created + timedelta(minutes=7),
                                    text="Добрый день! Подскажите, какой у вас бюджет?"))
                msgs.append(Message(direction=MessageDirection.inbound, created_at=created + timedelta(minutes=12),
                                    text="Около 100 тысяч в месяц"))
            if rnd.random() < 0.4:
                msgs.append(Message(direction=MessageDirection.outbound, kind=MessageKind.note,
                                    created_at=created + timedelta(hours=1), text="Созвонились, отправили КП"))
            lead.messages = msgs
            session.add(lead)
        await session.commit()
    print(f"Seeded {count} leads and {len(tags)} new tags")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete existing leads and tags first")
    parser.add_argument("-n", "--count", type=int, default=40, help="number of leads (default: 40)")
    args = parser.parse_args()
    asyncio.run(seed(args.reset, args.count))


if __name__ == "__main__":
    main()
