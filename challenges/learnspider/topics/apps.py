from django.apps import AppConfig


class TopicsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "topics"

    def ready(self):
        # 本地测试：不启动新闻采集调度（避免依赖外部站点/驱动）
        try:
            from LearnSpider.settings import DJANGO_ENV
        except Exception:
            DJANGO_ENV = "local"
        if DJANGO_ENV != "local":
            from topics.scheduler import start_scheduler
            start_scheduler()
