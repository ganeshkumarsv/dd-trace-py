class MyException(Exception):
    pass


def job_add1(x):
    return x + 1


def job_fail():
    raise MyException("error")
