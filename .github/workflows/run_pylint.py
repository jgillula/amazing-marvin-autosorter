import sys
from pylint.lint import Run
import pylint

print("Running pylint version {}".format(pylint.__version__))

# Define thresholds: <3=red, <6=orange <8=yellow <9.5=green <10=brightgreen
thresholds = {3: 'red',
              6: 'orange',
              8: 'yellow',
              9.5: 'green',
              10: 'brightgreen'}

results = Run(['--disable=line-too-long,missing-module-docstring,invalid-name,use-a-generator,global-statement,consider-using-generator,consider-using-enumerate,unnecessary-lambda,consider-using-set-comprehension,consider-using-dict-comprehension', 'main.py'], exit=False)

if results.linter.stats.fatal + results.linter.stats.error + results.linter.stats.warning > 0:
    print("##[set-output name=rating]failing!")
    print("##[set-output name=color]red")
    print("##[set-output name=linting_status]failed")
else:
    rating = results.linter.stats.global_note
    print("##[set-output name=rating]{:.2f}".format(rating))
    for value in thresholds.keys():
        if rating <= value:
            print("##[set-output name=color]{}".format(thresholds[value]))
            break
    print("##[set-output name=linting_status]passed")
sys.exit(0)
