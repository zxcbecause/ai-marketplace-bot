from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from config import settings

USER_COMMANDS = [
    BotCommand(command="card",    description="Полная карточка товара"),
    BotCommand(command="watermark", description="Наложить водяной знак магазина на фото"),
    BotCommand(command="desc",    description="Только описание"),
    BotCommand(command="chars",   description="Только характеристики"),
    BotCommand(command="fix",     description="Исправить последний результат"),
    BotCommand(command="retry",   description="Повторить последнее действие"),
    BotCommand(command="model",   description="Сменить модель LLM"),
    BotCommand(command="cancel",  description="Отменить текущую операцию"),
    BotCommand(command="myid",    description="Узнать свой user ID"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="wb_batch",    description="Батч WB-карточек с авто-шаблоном из API"),
    BotCommand(command="wb_create",   description="Создать карточки WB с нуля (артикул + название)"),
    BotCommand(command="wb_today",    description="Сколько карточек WB создано сегодня (09:30-18:30)"),
    BotCommand(command="adduser",    description="Добавить пользователя"),
    BotCommand(command="removeuser", description="Удалить пользователя"),
    BotCommand(command="users",      description="Список пользователей"),
    BotCommand(command="video",      description="Видеообложка WB: инфографика→прокрутка товара (+кадры/черновик/сборка)"),
    BotCommand(command="costs",      description="Траты LLM: сегодня / неделя / месяц / всего"),
    BotCommand(command="health",     description="Состояние систем: балансы, API, диск"),
    BotCommand(command="shutdown",   description="Выключить ПК (через 30 сек)"),
    BotCommand(command="cancelshutdown", description="Отменить выключение ПК"),
    BotCommand(command="restart_bot", description="Перезапустить бота (не весь ПК)"),
]


async def set_commands(bot: Bot):
    # Команды для всех пользователей
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())

    # Расширенные команды для администратора. На новом боте, которому админ
    # ещё ни разу не писал, Telegram не даёт задать команды на приватный чат
    # ("chat not found") — не даём этому уронить запуск бота целиком, админ
    # получит расширенное меню после первого /start.
    try:
        await bot.set_my_commands(
            ADMIN_COMMANDS,
            scope=BotCommandScopeChat(chat_id=settings.admin_id)
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Не удалось задать админ-команды (обычно значит: админ ещё не писал боту): {e}"
        )
