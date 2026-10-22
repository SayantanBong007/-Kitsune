class BaseModel:
    """Common behaviour for persisted models."""

    table = ""

    def to_dict(self):
        return dict(self.__dict__)

    def save(self):
        raise NotImplementedError
