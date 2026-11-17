package mylib.scenarios

import mylib.deployments.DeploymentStrategy
import mylib.steps.BaseSteps

/**
 * FreshInstallScenario executes a complete fresh installation workflow.
 */
class FreshInstallScenario implements Serializable {

    void setup(Object context, DeploymentStrategy deployment) {
        def script = context.script
        script.stage('Setup') {
            // setup tasks
        }
    }

    void execute(Object context, DeploymentStrategy deployment) {
        def script = context.script

        script.stage('Provision') {
            context.provisionInfo = deployment.provision(context)
        }

        script.stage('Install') {
            context.installInfo = deployment.install(context, context.provisionInfo)
        }
    }

}
