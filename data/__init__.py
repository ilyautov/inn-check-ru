# Пустой маркер пакета: data/ едет в колесо PyPI как пакет inn_check_ru_data
# (pyproject.toml: package-dir + package-data "*.json"), чтобы установленный
# droblenie_check находил канон порогов через
# importlib.resources.files("inn_check_ru_data") / "canon_ru.json".
# Из репозитория и из ZIP скилла канон по-прежнему читается по пути
# ../data/canon_ru.json; этот файл там ни на что не влияет.
