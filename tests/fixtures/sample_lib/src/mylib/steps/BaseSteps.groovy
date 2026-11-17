package mylib.steps

/**
 * Abstract base class for pipeline step implementations.
 */
abstract class BaseSteps {

    protected Object script
    protected Map paramsWrapper

    BaseSteps(Object script, Map paramsWrapper) {
        this.script = script
        this.paramsWrapper = paramsWrapper
    }

    protected def virtualStage(String name, Closure cl) {
        script.echo(">>>>>>>>>>  ${name}")
        try {
            return cl()
        } finally {
            script.echo("<<<<<<<<<< ${name}")
        }
    }

    protected def shWithUserEnv(String shellScript) {
        script.sh(shellScript)
    }

    def collectXUnitXmlFile(String junitXmlFile) {
        script.sh """
            mkdir -p "\${WORKSPACE}/xunit_reports"
            cp ${junitXmlFile} "\${WORKSPACE}/xunit_reports/"
        """
    }

}
