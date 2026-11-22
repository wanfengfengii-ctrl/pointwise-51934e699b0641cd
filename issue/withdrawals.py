from issue.packages import make_withdrawal_package, require_operation_id
from issue.register import IssueRegister


def withdraw(register: IssueRegister, withdrawal_id, correction_id):
    require_operation_id(withdrawal_id)
    require_operation_id(correction_id)
    with register.transaction():
        target = register.find(correction_id)
        if target is None or target.package.kind != 'correction':
            raise LookupError(f'issued correction not found: {correction_id}')
        package = make_withdrawal_package(withdrawal_id, target.package)
        original = register.find(withdrawal_id)
        if original is not None:
            if original.package != package:
                raise ValueError(f'conflicting issue: {withdrawal_id}')
            return original.package.package_id
        if register.find_withdrawal(correction_id) is not None:
            raise ValueError(f'correction already has a withdrawal: {correction_id}')
        register.save(package)
    return withdrawal_id
