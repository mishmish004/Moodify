class MoodifyException(Exception):
    """Base exception for the application"""
    pass

class DatabaseConnectionError(MoodifyException):
    """"""
    pass

class AudioProcessingError(MoodifyException):
    """CLAP processing failed"""
    pass